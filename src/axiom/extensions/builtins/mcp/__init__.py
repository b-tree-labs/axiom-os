# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Built-in root MCP server.

One MCP server per Axiom node. Aggregates platform primitives + every
installed extension's declared `[extension.mcp]` capabilities into a
single deterministic surface served over stdio (HTTP/SSE: Phase 5).

Spec: ``docs/specs/spec-builtin-mcp-server.md``
ADR:  ``docs/adrs/adr-038-builtin-mcp-server.md``
PRD:  ``docs/prds/prd-builtin-mcp-server.md``
"""

from __future__ import annotations

from axiom.extensions.builtins.mcp.aggregation import (
    AggregationRegistry,
    ExtensionContribution,
    MCPSurface,
)
from axiom.extensions.builtins.mcp.manifest_schema import (
    MCPExtensionConfig,
    MCPToolDecl,
    parse_mcp_block,
)

__all__ = [
    "AggregationRegistry",
    "ExtensionContribution",
    "MCPExtensionConfig",
    "MCPSurface",
    "MCPToolDecl",
    "parse_mcp_block",
]
