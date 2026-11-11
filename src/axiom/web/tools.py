# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Server-side web tools the ingress injects + executes in-enclave, so a routed
model gets web search transparently (the user is none the wiser) — replacing a
cloud client's native web tools, which can't run off their own backend.

The ingress strips the client's native web tools, injects these, and when the
model calls one it runs here (in the enclave, which has internet) and feeds the
result back into the same turn. Other tool calls pass through to the client.

EC guard: the query/URL leaves the enclave to the search provider, so it is
classified first; a controlled query is withheld (the model is told to rephrase
without controlled specifics) rather than silently egressed.
"""
from __future__ import annotations

import json
from typing import Any

__all__ = [
    "WEB_TOOL_NAMES",
    "web_tool_defs",
    "execute_web_tool",
    "is_web_tool",
]

WEB_TOOL_NAMES = ("web_search", "web_fetch")


def is_web_tool(name: str) -> bool:
    return name in WEB_TOOL_NAMES


def web_tool_defs() -> list[dict[str, Any]]:
    """OpenAI-chat-shape tool defs (the gateway's native shape) for injection."""
    return [
        {"type": "function", "function": {
            "name": "web_search",
            "description": (
                "Search the public web and return ranked results (title, url, "
                "snippet). Use for current information not in context. Do NOT "
                "include export-controlled or otherwise sensitive specifics in "
                "the query — it leaves to a search provider."
            ),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query."},
                "k": {"type": "integer", "description": "Max results (default 5)."},
            }, "required": ["query"]},
        }},
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": "Fetch a web page and return its text content.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "Absolute URL to fetch."},
            }, "required": ["url"]},
        }},
    ]


def _ec_blocked(router: Any, text: str) -> str | None:
    """Return a refusal reason if the text classifies export-controlled, else None.
    Fail-open is NOT acceptable here (content egresses), but a classifier outage
    should not silently leak — so on classifier error we BLOCK."""
    if router is None:
        return None
    try:
        from axiom.llm.router import RoutingTier

        decision = router.classify(text)
        if decision.tier == RoutingTier.EXPORT_CONTROLLED:
            terms = ", ".join(decision.matched_terms[:3]) if decision.matched_terms else ""
            return f"withheld: query classified export-controlled{(' (' + terms + ')') if terms else ''}"
    except Exception as exc:  # noqa: BLE001
        return f"withheld: could not classify query for export-control ({type(exc).__name__})"
    return None


def execute_web_tool(name: str, arguments: dict[str, Any], *, router: Any = None) -> dict[str, Any]:
    """Run a web tool in-enclave with the EC query-guard. Returns a JSON-able
    result the caller feeds back to the model as a tool result."""
    from axiom.web import search as websearch

    if name == "web_search":
        query = str(arguments.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "empty query"}
        blocked = _ec_blocked(router, query)
        if blocked:
            return {"ok": False, "error": blocked,
                    "guidance": "Rephrase without export-controlled specifics, or proceed without web search."}
        k = int(arguments.get("k", 5) or 5)
        return websearch.search(query, k=k)

    if name == "web_fetch":
        url = str(arguments.get("url", "")).strip()
        if not url:
            return {"ok": False, "error": "empty url"}
        blocked = _ec_blocked(router, url)
        if blocked:
            return {"ok": False, "error": blocked}
        return websearch.fetch(url)

    return {"ok": False, "error": f"unknown web tool: {name}"}


def tool_result_text(result: dict[str, Any]) -> str:
    """Compact text form of a web-tool result for feeding back to the model."""
    return json.dumps(result, default=str)[:6000]
