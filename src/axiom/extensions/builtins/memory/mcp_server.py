# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axiom-memory MCP server — cross-tool memory ledger over stdio.

Exposes the per-principal memory ledger to any MCP-aware client (Claude
Code, ChatGPT via connectors, Gemini, OpenCode, future tools). Read tools
recall prior conversation turns; write tools log new ones.

Reads and writes both flow through the same backend that ``axi memory show``
and ``axi memory record`` use — ``CompositionService`` on the SQLite-backed
``ArtifactRegistry`` rooted in the user state directory. PyPI install and
editable repo install behave identically.

Tools:

  axiom_memory_append    — record a single conversation turn (write)
  axiom_memory_show      — list a principal's recent fragments (read)
  axiom_memory_recent    — N most-recent turns for a principal (read)
  axiom_memory_search    — filter by tool / fact_kind / substring (read)

The server's `instructions` field is the primary mechanism driving model
discipline on writes. It is set during MCP initialization and shown to the
model so it knows to call ``axiom_memory_append`` after substantive turns.

Invocation:

    {
      "mcpServers": {
        "axiom-memory": {
          "command": "python",
          "args": ["-m", "axiom.extensions.builtins.memory.mcp_server"]
        }
      }
    }

No auth in this MVP — stdio local-only; identity comes from the local
state directory's principal binding. Cross-host access goes through the
federation gateway (Stage 5 of ADR-033, post-Prague).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from mcp.server import Server
from mcp.server.lowlevel.server import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import ServerCapabilities, TextContent, Tool, ToolsCapability

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService


# ---------------------------------------------------------------------------
# Server identity + instructions for model discipline
# ---------------------------------------------------------------------------


SERVER_NAME = "axiom-memory"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
You have access to the user's persistent cross-tool memory ledger via this
MCP server. Use it to:

1. **At the start of a substantive task**, call `axiom_memory_recent` (or
   `axiom_memory_search`) to recall relevant prior context for the user.
   The ledger spans every tool the user has used (Claude Code, ChatGPT,
   Gemini, OpenCode, axi chat) — what was decided last week may be there.

2. **After each substantive turn**, call `axiom_memory_append` to log the
   exchange. "Substantive" means: the user shared a decision, a preference,
   a fact about themselves or their work, a project context update, or any
   exchange whose continuity will matter in a later session. Skip trivial
   greetings and routine clarifications.

3. **Do not** include PII or secrets in the `summary` field beyond what the
   user explicitly shared in conversation; the ledger is local-first but
   may flow to declared peers via federation.

4. **Provenance is mandatory.** Always pass the originating tool (e.g.
   `claude-code`, `chatgpt`, `gemini`, `axi-chat`) and the model id when
   known so cross-tool queries can scope by origin.

The ledger is the user's, not yours — write what helps them recover state
across sessions, not what helps you.
"""


# ---------------------------------------------------------------------------
# CompositionService construction — mirror of cli.py's _build_default_composition
# ---------------------------------------------------------------------------


def _build_default_composition() -> CompositionService:
    """Default-runtime CompositionService rooted in the user state dir.

    Same shape as ``cli.py::_build_default_composition``. Tests replace
    this via monkeypatch with a tmp-path-isolated service.
    """
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.infra.paths import get_user_state_dir
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import Keypair, generate_keypair

    base = get_user_state_dir() / "memory"
    base.mkdir(parents=True, exist_ok=True)
    key_path = base / "node.key"
    if key_path.exists():
        kp = Keypair.from_private_bytes(key_path.read_bytes())
    else:
        kp = generate_keypair()
        key_path.write_bytes(kp.export_private())

    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


# ---------------------------------------------------------------------------
# Pure tool functions — direct callable for tests; wrapped for MCP transport
# ---------------------------------------------------------------------------


def append(
    *,
    tool: str,
    principal_id: str | None = None,
    user_input: str = "",
    assistant_output: str = "",
    model: str | None = None,
    summary: str | None = None,
    scope: str = "user",
    extra: dict[str, Any] | None = None,
) -> dict:
    """Record a single conversation turn. Cross-tool common path.

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import (
        record_session_turn,
        resolve_principal_id,
    )

    resolved_principal = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    frag = record_session_turn(
        composition=composition,
        principal_id=resolved_principal,
        tool=tool,
        user_input=user_input,
        assistant_output=assistant_output,
        model=model,
        summary=summary,
        scope=scope,
        extra=extra,
    )
    return {
        "fragment_id": frag.id,
        "principal_id": resolved_principal,
        "tool": tool,
        "model": model or "",
        "event_time": frag.content.get("event_time", ""),
    }


def show(*, principal_id: str | None = None, limit: int = 10) -> dict:
    """List a principal's recent fragments (with composed summary).

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import resolve_principal_id
    from axiom.memory.session_summary import (
        build_session_memory_summary,
        list_fragments_by_principal,
    )

    resolved = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    fragments = list_fragments_by_principal(
        composition, resolved, limit=limit,
    )
    summary = build_session_memory_summary(
        composition, resolved, max_fragments=limit,
    )
    return {
        "principal": resolved,
        "fragment_count": len(fragments),
        "summary": summary,
        "fragments": [_fragment_to_dict(f) for f in fragments],
    }


def recent(*, principal_id: str | None = None, n: int = 5) -> dict:
    """Return the N most-recent turns for a principal.

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import resolve_principal_id
    from axiom.memory.session_summary import list_fragments_by_principal

    resolved = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    fragments = list_fragments_by_principal(
        composition, resolved, limit=n,
    )
    return {
        "principal": resolved,
        "fragments": [_fragment_to_dict(f) for f in fragments],
    }


def search(
    *,
    principal_id: str | None = None,
    tool: str | None = None,
    fact_kind: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict:
    """Filter a principal's fragments by tool / fact_kind / substring.

    ``principal_id`` falls back to ``memory.default_principal`` when omitted.
    """
    from axiom.memory.session_capture import resolve_principal_id
    from axiom.memory.session_summary import list_fragments_by_principal

    resolved = resolve_principal_id(principal_id)
    composition = _build_default_composition()
    fragments = list_fragments_by_principal(
        composition, resolved, limit=limit,
    )

    def _matches(f) -> bool:
        if tool and f.content.get("tool") != tool:
            return False
        if fact_kind and f.content.get("fact_kind") != fact_kind:
            return False
        if query:
            q = query.lower()
            blob = " ".join([
                str(f.content.get("user_input", "")),
                str(f.content.get("assistant_output", "")),
                str(f.content.get("summary", "")),
            ]).lower()
            if q not in blob:
                return False
        return True

    matched = [f for f in fragments if _matches(f)]
    return {
        "principal": resolved,
        "fragments": [_fragment_to_dict(f) for f in matched],
    }


def _fragment_to_dict(frag) -> dict:
    return {
        "id": frag.id,
        "cognitive_type": frag.cognitive_type.value,
        "timestamp": frag.provenance.timestamp,
        "tool": frag.content.get("tool", ""),
        "model": frag.content.get("model", ""),
        "fact_kind": frag.content.get("fact_kind", ""),
        "summary": frag.content.get("summary", ""),
        "user_input": frag.content.get("user_input", ""),
        "assistant_output": frag.content.get("assistant_output", ""),
    }


# ---------------------------------------------------------------------------
# MCP tool descriptors
# ---------------------------------------------------------------------------


_TOOLS: list[Tool] = [
    Tool(
        name="axiom_memory_append",
        description=(
            "Record a single conversation turn into the user's cross-tool "
            "memory ledger. Call this after substantive exchanges (decisions, "
            "preferences, context, facts) so future sessions in any tool can "
            "recall what was said. Always pass the originating `tool` so "
            "downstream queries can scope by origin."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "principal_id": {
                    "type": "string",
                    "description": (
                        "The user (e.g. 'user@example.org'). Optional — "
                        "falls back to memory.default_principal setting "
                        "when omitted."
                    ),
                },
                "tool": {
                    "type": "string",
                    "description": (
                        "Originating tool — 'claude-code', 'chatgpt', "
                        "'gemini', 'opencode', 'axi-chat'."
                    ),
                },
                "user_input": {
                    "type": "string",
                    "description": "The user's prompt for this turn.",
                },
                "assistant_output": {
                    "type": "string",
                    "description": "The assistant's response text.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model id ('opus-4-7', 'gpt-4', 'gemini-2-flash'). "
                        "Optional but recommended."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "Compact one-line summary for prompt-injection in "
                        "future sessions. Auto-generated if omitted."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Logical scope; defaults to 'user' for personal "
                        "cross-tool memory."
                    ),
                },
                "extra": {
                    "type": "object",
                    "description": (
                        "Free-form metadata (session_id, host, cwd, etc.)."
                    ),
                },
            },
            "required": ["tool"],
        },
    ),
    Tool(
        name="axiom_memory_show",
        description=(
            "List a principal's recent memory fragments along with the "
            "composed session-memory summary that would inject into the "
            "next turn's prompt context. Read-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "principal_id": {
                    "type": "string",
                    "description": "The user to look up.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum fragments to return (default 10).",
                },
            },
        },
    ),
    Tool(
        name="axiom_memory_recent",
        description=(
            "Return the N most-recent conversation turns for a principal. "
            "Use this at session start to recall relevant prior context."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "principal_id": {"type": "string"},
                "n": {
                    "type": "integer",
                    "description": "Number of recent fragments (default 5).",
                },
            },
        },
    ),
    Tool(
        name="axiom_memory_search",
        description=(
            "Filter a principal's memory by originating tool, fact_kind, "
            "or substring match against turn text."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "principal_id": {"type": "string"},
                "tool": {
                    "type": "string",
                    "description": "Filter to fragments from this tool.",
                },
                "fact_kind": {
                    "type": "string",
                    "description": "Filter by fact_kind (e.g. 'chat_turn').",
                },
                "query": {
                    "type": "string",
                    "description": "Substring match against user_input + "
                                   "assistant_output + summary.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Initial scan limit (default 50).",
                },
            },
        },
    ),
]


_HANDLERS: dict = {
    "axiom_memory_append": lambda args: append(**args),
    "axiom_memory_show": lambda args: show(**args),
    "axiom_memory_recent": lambda args: recent(**args),
    "axiom_memory_search": lambda args: search(**args),
}


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------


def build_server() -> Server:
    """Construct the axiom-memory MCP Server with tools + handlers wired."""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()  # type: ignore[misc]
    async def _list_tools() -> list[Tool]:
        return _TOOLS

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[TextContent]:
        handler = _HANDLERS.get(name)
        if handler is None:
            return [TextContent(type="text", text=json.dumps({
                "error": f"unknown tool: {name}",
            }))]
        try:
            result = handler(arguments or {})
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


def _initialization_options(server: Server) -> InitializationOptions:
    """Build initialization options carrying our `instructions` field."""
    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=ServerCapabilities(tools=ToolsCapability()),
        instructions=INSTRUCTIONS,
    )


async def run() -> None:
    """Serve MCP over stdio. Entry point for `python -m <this module>`."""
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, _initialization_options(server),
        )


if __name__ == "__main__":
    asyncio.run(run())
