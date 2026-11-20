<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# mcp

**One MCP server per node. Every extension's published tools, on one surface.**

The server reads each extension's `[extension.mcp]` block, merges those contributions with the platform primitives, and serves the result. Stdio is the default. `POST` traffic for MCP-over-HTTP is the `/mcp` mount on the composed app. This extension's own block is `enabled = false`. The root server does not contribute tools to itself.

Conformance is [AEOS 0.1](../../../../../docs/specs/spec-aeos-0.1.md), version 0.1.0, `builtin = true`. The spec is `docs/specs/spec-builtin-mcp-server.md`.

## Where it sits

```mermaid
flowchart TD
  EXTS["Installed extensions<br/>extension.mcp blocks"]:::neutral
  PLAT["Platform primitives<br/>always on"]:::core
  REG["AggregationRegistry<br/>from_node then build"]:::accent
  SURF["MCPSurface<br/>tools resources prompts<br/>content_hash"]:::data
  STDIO["stdio server<br/>axi mcp serve"]:::core
  HTTP["http extension<br/>composed app"]:::core
  MOUNT["services/http_mount.py<br/>/mcp"]:::accent
  HOOK["hooks/subscriber.py<br/>post_install post_update post_uninstall"]:::vega
  CACHE["Surface cache"]:::neutral

  EXTS --> REG
  PLAT --> REG
  REG --> SURF
  SURF --> STDIO
  SURF --> MOUNT
  HTTP --> MOUNT
  HOOK --> REG
  HOOK --> CACHE
  SURF --> CACHE
```

The HTTP mount switches with the http app: `blue-green`, readiness `http:/healthz`. The hook `fail_mode` is `warn`. A failed refresh does not block install.

## Pieces

```mermaid
flowchart TD
  MAN["axiom-extension.toml<br/>cmd hook service<br/>extension.mcp enabled false"]:::neutral
  CLI["commands/cli.py<br/>serve status list-tools<br/>regenerate clients inspect"]:::core
  AGG["aggregation.py<br/>AggregationRegistry MCPSurface"]:::accent
  HOOK["hooks/subscriber.py<br/>on_extension_changed"]:::vega
  SVC["services/http_mount.py<br/>mcp_mount_spec"]:::accent
  GATE["identity_gate.py<br/>allowed principals"]:::vega
  TOOL["tools/<br/>describe_surface tool_owner"]:::data
  LIB["package __all__<br/>AggregationRegistry MCPSurface<br/>parse_mcp_block and the toolkit"]:::data

  MAN --> CLI
  MAN --> HOOK
  MAN --> SVC
  CLI --> AGG
  HOOK --> AGG
  SVC --> AGG
  AGG --> GATE
  LIB --> AGG
  LIB --> TOOL
  TOOL --> AGG
```

| Piece | What it is |
|---|---|
| `commands/cli.py` | `axi mcp`. Serve, status, list-tools, inspect, regenerate, clients. |
| `aggregation.py` | Builds `MCPSurface`. Same inputs, same `content_hash`. |
| `hooks/subscriber.py` | Regenerates the cache when the installed set changes. |
| `services/http_mount.py` | MCP-over-HTTP on the composed app. `requires_authz` defaults to true. |
| `identity_gate.py` | Tool name to principal patterns. An unmapped tool fails closed to owner-only. |
| `tools/` | The toolkit. `describe_surface` and `tool_owner` call `MCPSurface.to_dict` and `handler_source`. |
| `__init__.py` | The library. The registry types plus the toolkit names. |

`cli.py`, `subscriber.py`, and `http_mount.py` at the extension root import those implementations.

## How a surface is built

```mermaid
flowchart TD
  WALK["Discover extensions"]:::neutral
  PARSE["Parse each extension.mcp block<br/>skip enabled false"]:::neutral
  PRIM["Add platform primitives"]:::core
  COLL{"Name collision"}:::vega
  WINP["Platform name wins<br/>extension entry dropped"]:::core
  WINX["Two extensions<br/>lexicographic first wins"]:::accent
  HASH["content_hash over<br/>name description inputSchema"]:::data
  SERVE["stdio and /mcp<br/>dispatch by tool name"]:::core

  WALK --> PARSE
  PARSE --> PRIM
  PRIM --> COLL
  COLL -->|platform vs extension| WINP
  COLL -->|extension vs extension| WINX
  WINP --> HASH
  WINX --> HASH
  HASH --> SERVE
```

Manifest tools take `inputSchema` from `schema_inference`: an inline table, then `input_schema_module`, then the freeform blob. Registry tools take it from the shared projector. A schema change is a surface change, because the hash covers the schema.

## Toolkit and library

```python
from axiom.extensions.builtins.mcp import describe_surface, tool_owner
```

`describe_surface` returns `to_dict()` plus `tool_count`. `tool_owner` returns the contribution that owns a tool name, or `None`. `AggregationRegistry` and `MCPSurface` are the types those functions sit on.

Importing the package imports `AggregationRegistry`, which imports the `mcp` SDK. Importing `axiom.extensions.builtins.mcp.tools` does not. That module only calls `to_dict` and `handler_source`.
