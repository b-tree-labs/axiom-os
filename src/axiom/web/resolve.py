# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Server-side web-tool resolution for the ingress.

The model gets ``web_search``/``web_fetch`` injected; when it calls one, we run
it in-enclave and feed the result back into the SAME turn, looping until the
model produces a final answer (or a *client* tool call we must hand back). The
client never sees the web tool calls — the user is none the wiser.

Two entry points mirror the gateway:

- ``complete_with_web_tools`` — non-streaming; returns the final
  ``CompletionResponse`` (web calls already resolved).
- ``resolve_stream`` — yields gateway ``StreamChunk``s with web-tool chunks
  filtered out and turns stitched together, so the existing SSE translators
  consume it unchanged.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from axiom.web.tools import execute_web_tool, is_web_tool, tool_result_text, web_tool_defs

__all__ = ["complete_with_web_tools", "resolve_stream", "inject_web_tools"]

_MAX_HOPS = 8  # bound the internal web-tool loop


def _strip_web_tool_use(resp: Any) -> Any:
    """Remove any web tool_use blocks from a response so they NEVER reach the
    client (the client can't execute our in-enclave web tools). Applied to
    every value returned to the caller — incl. the hop-budget-exhausted case."""
    tu = getattr(resp, "tool_use", None)
    if tu:
        kept = [t for t in tu if not is_web_tool(t.name)]
        try:
            resp.tool_use = kept
        except Exception:  # noqa: BLE001 — frozen/odd response objects
            pass
    return resp


def inject_web_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Add the web tool defs to the client's tool list (native web tools were
    already stripped upstream)."""
    return [*(tools or []), *web_tool_defs()]


def _assistant_toolcalls_msg(text: str, calls: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [{
            "id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": c["args"]},
        } for c in calls],
    }


def complete_with_web_tools(gateway: Any, kw: dict[str, Any], *, routing_tier: str,
                            prefer: str | None, router: Any, routing_decision: Any) -> Any:
    """Non-streaming resolve loop. Returns the final CompletionResponse."""
    messages = list(kw["messages"])
    tools = inject_web_tools(kw.get("tools"))
    resp = None
    for _ in range(_MAX_HOPS):
        resp = gateway.complete_with_tools(
            messages, system=kw["system"], tools=tools, max_tokens=kw["max_tokens"],
            routing_tier=routing_tier, routing_decision=routing_decision, prefer=prefer,
        )
        web = [tu for tu in (getattr(resp, "tool_use", None) or []) if is_web_tool(tu.name)]
        other = [tu for tu in (getattr(resp, "tool_use", None) or []) if not is_web_tool(tu.name)]
        if not web or other:
            # Final turn: text and/or a client tool call we hand back. Strip any
            # web tool_use so it never leaks (other client tools are preserved).
            return _strip_web_tool_use(resp)
        calls = [{"id": tu.tool_id, "name": tu.name, "args": json.dumps(tu.input)} for tu in web]
        messages.append(_assistant_toolcalls_msg(getattr(resp, "text", "") or "", calls))
        for tu in web:
            result = execute_web_tool(tu.name, tu.input, router=router)
            messages.append({"role": "tool", "tool_call_id": tu.tool_id,
                             "content": tool_result_text(result)})
    return _strip_web_tool_use(resp)  # hop budget exhausted — never leak web calls


def resolve_stream(gateway: Any, kw: dict[str, Any], *, routing_tier: str,
                   prefer: str | None, router: Any) -> Iterator[Any]:
    """Streaming resolve loop. Yields StreamChunks for the client with web-tool
    chunks suppressed and follow-up turns stitched in. Emits exactly one final
    ``done``."""
    messages = list(kw["messages"])
    tools = inject_web_tools(kw.get("tools"))

    for _ in range(_MAX_HOPS):
        # Per-turn web-call accumulation; suppressed from the client stream.
        web_calls: list[dict[str, str]] = []
        cur: dict[str, str] | None = None
        suppressing = False
        assistant_text: list[str] = []
        had_client_tool = False

        for ch in gateway.stream_with_tools(
            messages, system=kw["system"], tools=tools, max_tokens=kw["max_tokens"],
            routing_tier=routing_tier, prefer=prefer,
        ):
            ctype = getattr(ch, "type", "")
            if ctype == "tool_use_start":
                if is_web_tool(getattr(ch, "tool_name", "")):
                    suppressing = True
                    cur = {"id": ch.tool_id, "name": ch.tool_name, "args": ""}
                    continue  # suppress from client
                had_client_tool = True
                yield ch
            elif ctype == "tool_input_delta":
                if suppressing and cur is not None:
                    cur["args"] += ch.tool_input_json
                    continue
                yield ch
            elif ctype == "tool_use_end":
                if suppressing and cur is not None:
                    web_calls.append(cur)
                    cur = None
                    suppressing = False
                    continue
                yield ch
            elif ctype == "text":
                assistant_text.append(ch.text)
                yield ch
            elif ctype == "done":
                # If this turn only made web calls (no client tool), resolve and
                # continue WITHOUT emitting done; else this is the final turn.
                if web_calls and not had_client_tool:
                    break  # fall through to resolve + next hop
                yield ch
                return
            else:
                yield ch  # thinking/usage/etc. pass through

        if not web_calls:
            return  # stream ended without a done? stop safely
        # Resolve the web calls in-enclave, append, loop for the continuation.
        messages.append(_assistant_toolcalls_msg("".join(assistant_text), web_calls))
        for c in web_calls:
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute_web_tool(c["name"], args, router=router)
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": tool_result_text(result)})
    # hop budget exhausted
    from axiom.llm.gateway import StreamChunk

    yield StreamChunk(type="done")
