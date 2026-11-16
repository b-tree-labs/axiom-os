#!/usr/bin/env python3
# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Quick (T, U, A, R) audit dump for the local Axiom node's memory store
via the same MCP server CC / Codex / Goose use.

Usage:
    python scripts/inspect-mcp-memory.py [principal] [limit]

Defaults: principal=@laptop:ben, limit=5.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(principal: str, limit: int) -> int:
    params = StdioServerParameters(
        # Use the current interpreter (the venv running this script) by default;
        # override with AXI_MCP_PYTHON to point at a different environment.
        command=os.environ.get("AXI_MCP_PYTHON", sys.executable),
        args=["-m", "axiom.extensions.builtins.mcp.server"],
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(
                "axiom_memory__retrieve",
                arguments={"principal": principal, "limit": limit},
            )
            payload = json.loads(res.content[0].text)
            for f in payload["fragments"]:
                print(f"\n=== {f['timestamp'][:19]}  {f['fact_kind']}")
                print(f"  summary:       {f['summary']}")
                p = f.get("provenance", {})
                print(f"  T (time):      {p.get('timestamp', '')[:19]}")
                print(f"  U (principal): {p.get('principal_id')}")
                print(f"  A (agents):    {p.get('agents')}")
                print(f"  R (resources): {p.get('resources')}")
    return 0


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else "@laptop:ben"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    sys.exit(asyncio.run(main(pid, n)))
