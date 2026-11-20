# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Anything this server advertises, a client can use.

The server registered `on_list_prompts` and never `on_get_prompt`. So every
published prompt appeared in a harness's slash menu and errored the moment
somebody chose it:

    /axiom:telemetry.monitor-authoring (MCP)
      ProtocolError: Method not found

A colleague hit it during onboarding on 2026-10-01, four times in a row,
and restarting the editor appeared to fix it because he stopped using the
menu. It had never worked, in any release.

The listing is what a test watches, and the listing was fine, which is why
this survived. So the guard here is a conformance one rather than a case:
**everything the surface advertises must be reachable by the call a client
would make**, prompts included. A fake client that only lists is a fake
client that would have passed yesterday.
"""

from __future__ import annotations

import pytest
from mcp import types

from axiom.extensions.builtins.mcp.aggregation import MCPSurface
from axiom.extensions.builtins.mcp.server import build_server


def _surface_with_a_prompt() -> MCPSurface:
    """A surface advertising one prompt, rendered by the same dispatch entry
    the tool door already uses."""

    async def _get(args: dict) -> dict:
        name = (args or {}).get("name") or ""
        if name != "guidance":
            return {"ok": False, "error": f"no prompt named {name!r}"}
        return {"ok": True, "name": name, "content": "Read the runbook first."}

    from datetime import UTC, datetime

    return MCPSurface(
        tools=[],
        resources=[],
        prompts=[types.Prompt(name="guidance", description="How to start")],
        dispatch={"axiom_prompts__get": _get},
        content_hash="test",
        generated_at=datetime.now(UTC),
        sources=[],
    )


def test_the_server_answers_the_call_a_client_makes_for_a_prompt():
    """Looked up by METHOD, which is how the SDK keys handlers and how a
    client addresses one. Asserting on a request type instead passes against
    a server that answers nothing."""
    server = build_server(_surface_with_a_prompt())
    assert server.get_request_handler("prompts/get") is not None, (
        "nothing answers prompts/get, so every prompt this server advertises "
        "appears in a client's menu and fails when chosen"
    )


@pytest.mark.anyio
async def test_every_advertised_prompt_is_fetchable():
    """The conformance shape: walk what the server publishes and call it.

    A test that asserts the listing proves the menu, not the menu's entries.
    """
    surface = _surface_with_a_prompt()
    server = build_server(surface)
    entry = server.get_request_handler("prompts/get")
    assert entry is not None

    for prompt in surface.prompts:
        result = await entry.handler(None, types.GetPromptRequestParams(name=prompt.name))
        assert isinstance(result, types.GetPromptResult), result
        assert result.messages, f"{prompt.name} rendered no messages"
        assert "runbook" in result.messages[0].content.text


@pytest.mark.anyio
async def test_an_unknown_prompt_names_what_does_exist():
    """A dead end that lists the alternatives is the next step instead."""
    server = build_server(_surface_with_a_prompt())
    entry = server.get_request_handler("prompts/get")
    with pytest.raises(ValueError) as refused:
        await entry.handler(None, types.GetPromptRequestParams(name="not-a-prompt"))
    assert "not-a-prompt" in str(refused.value)


@pytest.fixture
def anyio_backend():
    return "asyncio"
