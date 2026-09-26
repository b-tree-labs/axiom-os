# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dual-protocol LLM ingress for the Axiom gateway (Anthropic + OpenAI).

A thin local HTTP service so any IDE/TUI can target one Axiom endpoint:
- ``/v1/messages`` — Anthropic Messages API (Claude Code, Claude Desktop).
- ``/v1/chat/completions`` + ``/v1/models`` — OpenAI API (Cursor, Codex,
  Continue, anything OpenAI-SDK; covers GPT models too).

Every request is **translated through the Axiom gateway**, so all clients ride
the same routing tiers, fail-closed export-controlled enforcement, vault, and
audit as ``axi chat`` — NOT a LiteLLM-style passthrough that would bypass the
chokepoint. Provider flip on the fly: the requested ``model`` field selects a
configured provider when it names one (tier-checked), else default routing;
``routing.prefer_provider`` is the live default.

Point a client at it with ``ANTHROPIC_BASE_URL=http://localhost:<port>``.

Routing tier defaults to ``any``; an export-controlled deployment can pin it
with ``AXIOM_BRIDGE_ROUTING_TIER=export_controlled`` so every request through
the bridge is held to the EC providers (fail-closed).

Translation only — no model selection here; the gateway picks the provider by
config. The requested Anthropic ``model`` is echoed back for client
compatibility.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Iterator
from typing import Any


def _flatten_system(system: Any) -> str:
    """Anthropic ``system`` is a str or a list of text blocks."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    parts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _content_to_parts(content: Any) -> list[dict[str, Any]]:
    """Normalize a message ``content`` into a list of Anthropic blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


# Tools that Anthropic executes server-side (not by the client, not by the
# model). Off real Anthropic they cannot run: the model "calls" them, gets
# nothing back, and loops until the stream errors. Strip them before forwarding
# so a non-Anthropic backend never attempts them. Override via
# AXIOM_BRIDGE_STRIP_TOOLS (comma-separated); set to empty to disable.
_DEFAULT_SERVER_SIDE_TOOLS = "WebSearch,WebFetch"


def _server_side_tools() -> frozenset[str]:
    raw = os.environ.get("AXIOM_BRIDGE_STRIP_TOOLS", _DEFAULT_SERVER_SIDE_TOOLS)
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def anthropic_to_gateway(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages request into gateway call kwargs.

    Returns ``{messages, system, tools, max_tokens}`` where ``messages`` and
    ``tools`` are in the OpenAI shape the gateway expects.
    """
    system = _flatten_system(body.get("system"))
    max_tokens = int(body.get("max_tokens", 4096))

    messages: list[dict[str, Any]] = []
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        blocks = _content_to_parts(msg.get("content"))

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                text_parts.append(b.get("text", ""))
            elif btype == "tool_use":  # assistant asked to call a tool
                tool_calls.append(
                    {
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {})),
                        },
                    }
                )
            elif btype == "tool_result":  # user returns a tool's output
                rc = b.get("content", "")
                if isinstance(rc, list):
                    rc = "\n".join(
                        p.get("text", "") for p in rc if isinstance(p, dict) and p.get("type") == "text"
                    )
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": rc if isinstance(rc, str) else json.dumps(rc),
                    }
                )

        # tool_result blocks become their own OpenAI 'tool' messages.
        if tool_results:
            messages.extend(tool_results)
        if text_parts or tool_calls:
            m: dict[str, Any] = {"role": role, "content": "\n".join(text_parts)}
            if tool_calls:
                m["tool_calls"] = tool_calls
            messages.append(m)

    tools = None
    if body.get("tools"):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object"}),
                },
            }
            for t in body["tools"]
            if t.get("name", "") not in _server_side_tools()
        ]

    return {
        "messages": messages,
        "system": system,
        "tools": tools,
        "max_tokens": max_tokens,
    }


def _stop_reason(resp: Any) -> str:
    if getattr(resp, "tool_use", None):
        return "tool_use"
    raw = (getattr(resp, "stop_reason", "") or "").lower()
    if raw in ("length", "max_tokens"):
        return "max_tokens"
    return "end_turn"


def completion_to_anthropic(resp: Any, model: str) -> dict[str, Any]:
    """Translate a gateway ``CompletionResponse`` into an Anthropic message."""
    content: list[dict[str, Any]] = []
    if getattr(resp, "text", ""):
        content.append({"type": "text", "text": resp.text})
    for tu in getattr(resp, "tool_use", []) or []:
        content.append(
            {"type": "tool_use", "id": tu.tool_id, "name": tu.name, "input": tu.input}
        )
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _stop_reason(resp),
        "stop_sequence": None,
        "usage": {
            "input_tokens": getattr(resp, "input_tokens", 0),
            "output_tokens": getattr(resp, "output_tokens", 0),
        },
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def stream_to_anthropic_sse(chunks: Iterable[Any], model: str) -> Iterator[str]:
    """Translate gateway ``StreamChunk`` deltas into the Anthropic SSE stream.

    Emits the Anthropic event sequence: message_start → (content blocks:
    text and/or tool_use, each start/delta*/stop) → message_delta → message_stop.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    index = -1
    open_kind: str | None = None  # "text" | "tool"
    out_tokens = 0
    stop_reason = "end_turn"

    def _close() -> Iterator[str]:
        nonlocal open_kind
        if open_kind is not None:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
            open_kind = None

    for ch in chunks:
        ctype = getattr(ch, "type", "")
        if ctype == "text":
            if open_kind != "text":
                yield from _close()
                index += 1
                open_kind = "text"
                yield _sse(
                    "content_block_start",
                    {"type": "content_block_start", "index": index,
                     "content_block": {"type": "text", "text": ""}},
                )
            yield _sse(
                "content_block_delta",
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": "text_delta", "text": ch.text}},
            )
        elif ctype == "tool_use_start":
            yield from _close()
            index += 1
            open_kind = "tool"
            stop_reason = "tool_use"
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "tool_use", "id": ch.tool_id,
                                   "name": ch.tool_name, "input": {}}},
            )
        elif ctype == "tool_input_delta":
            yield _sse(
                "content_block_delta",
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": "input_json_delta", "partial_json": ch.tool_input_json}},
            )
        elif ctype == "tool_use_end":
            yield from _close()
        elif ctype == "thinking_start":
            yield from _close()
            index += 1
            open_kind = "thinking"
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "thinking", "thinking": ""}},
            )
        elif ctype == "thinking_delta":
            if open_kind == "thinking":
                yield _sse(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index,
                     "delta": {"type": "thinking_delta", "thinking": getattr(ch, "text", "")}},
                )
        elif ctype == "thinking_end":
            yield from _close()
        elif ctype == "usage":
            out_tokens = getattr(ch, "output_tokens", 0) or out_tokens
        elif ctype == "done":
            break

    yield from _close()
    yield _sse(
        "message_delta",
        {"type": "message_delta",
         "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": out_tokens}},
    )
    yield _sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# OpenAI /v1/chat/completions translation
# ---------------------------------------------------------------------------


def openai_to_gateway(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completions request into gateway kwargs.

    The gateway is already OpenAI-shaped, so messages/tools pass through; we
    only lift ``system`` messages into the ``system`` field.
    """
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for m in body.get("messages", []):
        if m.get("role") == "system":
            c = m.get("content", "")
            system_parts.append(c if isinstance(c, str) else json.dumps(c))
        else:
            messages.append(m)
    strip = _server_side_tools()
    tools = body.get("tools")
    if tools:
        tools = [t for t in tools if t.get("function", {}).get("name", "") not in strip]
    return {
        "messages": messages,
        "system": "\n".join(p for p in system_parts if p),
        "tools": tools,
        "max_tokens": int(body.get("max_tokens", 4096)),
    }


def _openai_finish(resp: Any) -> str:
    if getattr(resp, "tool_use", None):
        return "tool_calls"
    raw = (getattr(resp, "stop_reason", "") or "").lower()
    return "length" if raw in ("length", "max_tokens") else "stop"


def completion_to_openai(resp: Any, model: str) -> dict[str, Any]:
    """Translate a gateway ``CompletionResponse`` into an OpenAI chat completion."""
    message: dict[str, Any] = {"role": "assistant", "content": getattr(resp, "text", "") or None}
    tcs = [
        {"id": tu.tool_id, "type": "function",
         "function": {"name": tu.name, "arguments": json.dumps(tu.input)}}
        for tu in getattr(resp, "tool_use", []) or []
    ]
    if tcs:
        message["tool_calls"] = tcs
    pt = getattr(resp, "input_tokens", 0)
    ct = getattr(resp, "output_tokens", 0)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": _openai_finish(resp)}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


def stream_to_openai_sse(chunks: Iterable[Any], model: str) -> Iterator[str]:
    """Translate gateway ``StreamChunk`` deltas into the OpenAI SSE chunk stream."""
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def _chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    yield _chunk({"role": "assistant"})
    tool_idx = -1
    finish = "stop"
    for ch in chunks:
        ctype = getattr(ch, "type", "")
        if ctype == "text":
            yield _chunk({"content": ch.text})
        elif ctype == "tool_use_start":
            tool_idx += 1
            finish = "tool_calls"
            yield _chunk({"tool_calls": [{"index": tool_idx, "id": ch.tool_id, "type": "function",
                                          "function": {"name": ch.tool_name, "arguments": ""}}]})
        elif ctype == "tool_input_delta":
            yield _chunk({"tool_calls": [{"index": tool_idx, "function": {"arguments": ch.tool_input_json}}]})
        elif ctype == "done":
            break
    yield _chunk({}, finish=finish)
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# OpenAI Responses API (/v1/responses) — Codex 0.129+ speaks ONLY this
# ---------------------------------------------------------------------------


def _responses_input_to_messages(inp: Any) -> list[dict[str, Any]]:
    """Translate a Responses ``input`` (string or item list) into chat messages."""
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    messages: list[dict[str, Any]] = []
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype in (None, "message"):
            content = item.get("content")
            if isinstance(content, str):
                text = content
            else:
                parts = []
                for c in content or []:
                    if isinstance(c, dict) and c.get("type") in (
                        "input_text", "output_text", "text", "summary_text",
                    ):
                        parts.append(c.get("text", ""))
                    elif isinstance(c, str):
                        parts.append(c)
                text = "\n".join(parts)
            messages.append({"role": item.get("role", "user"), "content": text})
        elif itype == "function_call":
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id", ""),
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "")},
                }],
            })
        elif itype == "function_call_output":
            out = item.get("output")
            messages.append({
                "role": "tool", "tool_call_id": item.get("call_id", ""),
                "content": out if isinstance(out, str) else json.dumps(out),
            })
    return messages


def responses_to_gateway(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI Responses request into gateway kwargs."""
    strip = _server_side_tools()
    tools: list[dict[str, Any]] | None = None
    raw = body.get("tools")
    if raw:
        tools = []
        for t in raw:
            # chat-completions accepts ONLY function tools. Responses clients
            # (Codex) also send native server-side tool types — web_search,
            # local_shell, etc. — which have no "function" key and 400 the
            # upstream ('tools[N] must have a "function" key'). Drop them: web
            # search is provided in-enclave by our injected tools; other native
            # types aren't representable in chat-completions.
            if t.get("type") != "function":
                continue
            # A Responses function tool is FLAT: {type:"function", name,
            # description, parameters[, strict]}. chat-completions wants it nested.
            name = t.get("name") or t.get("function", {}).get("name", "")
            if name in strip:
                continue
            if "function" not in t:
                fn: dict[str, Any] = {
                    "name": name,
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object"}),
                }
                if "strict" in t:
                    fn["strict"] = t["strict"]
                tools.append({"type": "function", "function": fn})
            else:
                tools.append(t)
        tools = tools or None
    return {
        "messages": _responses_input_to_messages(body.get("input")),
        "system": body.get("instructions") or "",
        "tools": tools,
        "max_tokens": int(body.get("max_output_tokens") or body.get("max_tokens") or 4096),
    }


def completion_to_responses(resp: Any, model: str) -> dict[str, Any]:
    """Translate a gateway ``CompletionResponse`` into an OpenAI Response object."""
    output: list[dict[str, Any]] = []
    text = getattr(resp, "text", "") or ""
    if text:
        output.append({
            "type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}", "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tu in getattr(resp, "tool_use", []) or []:
        output.append({
            "type": "function_call", "id": f"fc_{uuid.uuid4().hex[:24]}",
            "call_id": tu.tool_id, "name": tu.name,
            "arguments": json.dumps(tu.input), "status": "completed",
        })
    pt = getattr(resp, "input_tokens", 0)
    ct = getattr(resp, "output_tokens", 0)
    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}", "object": "response", "created_at": 0,
        "model": model, "status": "completed", "output": output,
        "usage": {"input_tokens": pt, "output_tokens": ct, "total_tokens": pt + ct},
    }


def stream_to_responses_sse(chunks: Iterable[Any], model: str) -> Iterator[str]:
    """Translate gateway ``StreamChunk`` deltas into the Responses SSE stream."""
    rid = f"resp_{uuid.uuid4().hex[:24]}"
    seq = 0

    def ev(event: str, data: dict[str, Any]) -> str:
        nonlocal seq
        payload = {**data, "sequence_number": seq}
        seq += 1
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    base = {"id": rid, "object": "response", "created_at": 0, "model": model,
            "status": "in_progress", "output": []}
    yield ev("response.created", {"response": base})
    yield ev("response.in_progress", {"response": base})

    out_index = 0
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    msg_open = False
    text_accum: list[str] = []
    final_output: list[dict[str, Any]] = []
    tools: dict[int, dict[str, str]] = {}
    cur_tool: int | None = None

    def _close_message() -> Iterator[str]:
        nonlocal msg_open, out_index
        full = "".join(text_accum)
        yield ev("response.output_text.done",
                 {"item_id": msg_id, "output_index": out_index, "content_index": 0, "text": full})
        yield ev("response.content_part.done",
                 {"item_id": msg_id, "output_index": out_index, "content_index": 0,
                  "part": {"type": "output_text", "text": full, "annotations": []}})
        item = {"type": "message", "id": msg_id, "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": full, "annotations": []}]}
        yield ev("response.output_item.done", {"output_index": out_index, "item": item})
        final_output.append(item)
        msg_open = False
        out_index += 1

    for ch in chunks:
        ctype = getattr(ch, "type", "")
        if ctype == "text":
            if not msg_open:
                msg_open = True
                yield ev("response.output_item.added", {"output_index": out_index, "item": {
                    "type": "message", "id": msg_id, "status": "in_progress",
                    "role": "assistant", "content": []}})
                yield ev("response.content_part.added", {
                    "item_id": msg_id, "output_index": out_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []}})
            text_accum.append(ch.text)
            yield ev("response.output_text.delta", {
                "item_id": msg_id, "output_index": out_index, "content_index": 0, "delta": ch.text})
        elif ctype == "tool_use_start":
            if msg_open:
                yield from _close_message()
            fc_id = f"fc_{uuid.uuid4().hex[:24]}"
            tools[out_index] = {"id": fc_id, "call_id": ch.tool_id, "name": ch.tool_name, "args": ""}
            cur_tool = out_index
            yield ev("response.output_item.added", {"output_index": out_index, "item": {
                "type": "function_call", "id": fc_id, "call_id": ch.tool_id,
                "name": ch.tool_name, "arguments": ""}})
        elif ctype == "tool_input_delta":
            if cur_tool is not None:
                tools[cur_tool]["args"] += ch.tool_input_json
                yield ev("response.function_call_arguments.delta", {
                    "item_id": tools[cur_tool]["id"], "output_index": cur_tool,
                    "delta": ch.tool_input_json})
        elif ctype == "tool_use_end":
            if cur_tool is not None:
                ti = tools[cur_tool]
                yield ev("response.function_call_arguments.done", {
                    "item_id": ti["id"], "output_index": cur_tool, "arguments": ti["args"]})
                item = {"type": "function_call", "id": ti["id"], "call_id": ti["call_id"],
                        "name": ti["name"], "arguments": ti["args"], "status": "completed"}
                yield ev("response.output_item.done", {"output_index": cur_tool, "item": item})
                final_output.append(item)
                out_index += 1
                cur_tool = None
        elif ctype == "done":
            break

    if msg_open:
        yield from _close_message()

    final = {"id": rid, "object": "response", "created_at": 0, "model": model,
             "status": "completed", "output": final_output,
             "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    yield ev("response.completed", {"response": final})


def classify_messages(router: Any, messages: list[dict[str, Any]]) -> Any:
    """Classify on already-translated messages (protocol-agnostic)."""
    text, context = _text_and_context(messages)
    return router.classify(text, session_mode=_session_mode(), context=context)


def _prefer_from_model(provider_names: set[str], model: str) -> str | None:
    """Flip by model field: if the requested model names a configured provider,
    prefer it (tier-checked downstream); otherwise None -> default routing."""
    return model if model in provider_names else None


def _route_path(raw: str) -> str:
    """Normalize a request path for routing: drop the query string (clients
    append e.g. ``/v1/messages?beta=true``) and any trailing slash."""
    return raw.split("?", 1)[0].rstrip("/")


def count_tokens_anthropic(body: dict[str, Any]) -> dict[str, int]:
    """Approximate Anthropic's ``/v1/messages/count_tokens`` (chars/4 heuristic).

    Claude Code calls this preflight before every turn; a 404 here surfaces to
    the user as "API Error". An estimate is sufficient for its context display
    and unblocks the turn (upstreams don't expose a uniform token-count API).
    """
    chunks: list[str] = []
    system = body.get("system")
    if isinstance(system, str):
        chunks.append(system)
    elif isinstance(system, list):
        chunks += [b.get("text", "") for b in system if isinstance(b, dict)]
    for m in body.get("messages", []):
        for part in _content_to_parts(m.get("content")):
            if part.get("type") == "text":
                chunks.append(part.get("text", ""))
    return {"input_tokens": max(1, len("".join(chunks)) // 4)}


# ---------------------------------------------------------------------------
# HTTP service
# ---------------------------------------------------------------------------


def _routing_tier() -> str:
    return os.environ.get("AXIOM_BRIDGE_ROUTING_TIER", "any")


def _web_tools_enabled() -> bool:
    """Server-side web tools (in-enclave search/fetch) are on by default so a
    routed model has web access transparently. Disable with
    AXIOM_BRIDGE_WEB_TOOLS=0."""
    return os.environ.get("AXIOM_BRIDGE_WEB_TOOLS", "1").strip().lower() not in ("0", "false", "no")


def _session_mode() -> str:
    """An EC deployment pins the tier via env (floor); else classify per request."""
    return "export_controlled" if _routing_tier() == "export_controlled" else "auto"


def _text_and_context(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Last user message text + prior turns as role/content dicts for the router."""
    flat: list[dict[str, str]] = []
    for m in messages:
        parts = _content_to_parts(m.get("content"))
        text = "\n".join(b.get("text", "") for b in parts if b.get("type") == "text")
        flat.append({"role": m.get("role", "user"), "content": text})
    last_user = ""
    for m in reversed(flat):
        if m["role"] == "user":
            last_user = m["content"]
            break
    return last_user, flat[:-1]


def classify_request(router: Any, body: dict[str, Any]) -> Any:
    """Run the shared ``QueryRouter`` so the ingress uses the SAME routing
    policy (keyword → SLM → fallback → tier) as the rest of the harness —
    rather than a static tier. Returns a ``RoutingDecision``.
    """
    text, context = _text_and_context(body.get("messages", []))
    return router.classify(text, session_mode=_session_mode(), context=context)


def _build_handler():
    from http.server import BaseHTTPRequestHandler

    from axiom.llm.gateway import Gateway
    from axiom.llm.router import QueryRouter

    gateway = Gateway()
    router = QueryRouter()
    provider_names = {p.name for p in gateway.providers}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # quiet by default
            pass

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = _route_path(self.path)
            if p in ("/health", "/v1/health"):
                self._json(200, {"status": "ok", "service": "axiom-gateway-ingress"})
            elif p == "/v1/models":
                self._json(200, {"object": "list", "data": [
                    {"id": n, "object": "model", "owned_by": "axiom"} for n in sorted(provider_names)
                ]})
            else:
                self._json(404, {"type": "error", "error": {"type": "not_found"}})

        def do_POST(self):
            path = _route_path(self.path)
            if path not in ("/v1/messages", "/v1/chat/completions",
                            "/v1/responses", "/v1/messages/count_tokens"):
                self._json(404, {"type": "error", "error": {"type": "not_found"}})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}})
                return

            # Anthropic token-count preflight (Claude Code calls this per turn).
            if path == "/v1/messages/count_tokens":
                self._json(200, count_tokens_anthropic(body))
                return

            if path == "/v1/messages":
                proto = "anthropic"
            elif path == "/v1/responses":
                proto = "responses"
            else:
                proto = "openai"

            model = body.get("model", "axiom-gateway")
            # Translate first, then classify on the normalized messages so the
            # SAME QueryRouter policy applies to every protocol (incl. Responses,
            # whose input lives under `input`, not `messages`). Gateway enforces
            # the tier (fail-closed EC). `prefer` flips provider by model field
            # but only among tier-allowed candidates — never an EC escape.
            if proto == "anthropic":
                kw = anthropic_to_gateway(body)
            elif proto == "responses":
                kw = responses_to_gateway(body)
            else:
                kw = openai_to_gateway(body)
            decision = classify_messages(router, kw["messages"])
            tier = decision.tier.value
            prefer = _prefer_from_model(provider_names, model)

            if body.get("stream"):
                # Close the connection after the stream. With HTTP/1.1 keep-alive
                # and no Content-Length/chunked terminator, the client cannot
                # detect end-of-response and hangs after the final SSE event
                # (Claude Code surfaces this as a stalled turn / API error).
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if _web_tools_enabled():
                        from axiom.web.resolve import resolve_stream
                        chunks = resolve_stream(gateway, kw, routing_tier=tier,
                                                prefer=prefer, router=router)
                    else:
                        chunks = gateway.stream_with_tools(
                            kw["messages"], system=kw["system"], tools=kw["tools"],
                            max_tokens=kw["max_tokens"], routing_tier=tier, prefer=prefer,
                        )
                    sse = {
                        "anthropic": stream_to_anthropic_sse,
                        "responses": stream_to_responses_sse,
                        "openai": stream_to_openai_sse,
                    }[proto]
                    for event in sse(chunks, model):
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                except Exception as exc:  # noqa: BLE001
                    if proto == "anthropic":
                        self.wfile.write(_sse("error", {"type": "error", "error": {"type": "api_error", "message": str(exc)}}).encode())
                    elif proto == "responses":
                        self.wfile.write(f"event: error\ndata: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n".encode())
                    else:
                        self.wfile.write(("data: " + json.dumps({"error": {"message": str(exc), "type": "api_error"}}) + "\n\ndata: [DONE]\n\n").encode())
            else:
                try:
                    if _web_tools_enabled():
                        from axiom.web.resolve import complete_with_web_tools
                        resp = complete_with_web_tools(
                            gateway, kw, routing_tier=tier, prefer=prefer,
                            router=router, routing_decision=decision,
                        )
                    else:
                        resp = gateway.complete_with_tools(
                            kw["messages"], system=kw["system"], tools=kw["tools"],
                            max_tokens=kw["max_tokens"], routing_tier=tier,
                            routing_decision=decision, prefer=prefer,
                        )
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"type": "error", "error": {"type": "api_error", "message": str(exc)}})
                    return
                out = {
                    "anthropic": completion_to_anthropic,
                    "responses": completion_to_responses,
                    "openai": completion_to_openai,
                }[proto](resp, model)
                self._json(200, out)

    return Handler


def serve(port: int = 8788, host: str = "127.0.0.1") -> None:
    """Run the ingress service (blocking)."""
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer((host, port), _build_handler())
    print(
        f"axiom anthropic-ingress on http://{host}:{port}  "
        f"(routing_tier={_routing_tier()}) — set ANTHROPIC_BASE_URL to this URL"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="axiom-anthropic-ingress")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AXIOM_BRIDGE_PORT", "8788")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    serve(port=args.port, host=args.host)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
