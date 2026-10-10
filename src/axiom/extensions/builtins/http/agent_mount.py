# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The opt-in mount for the agent-backed serving path (spec-serve §4.1).

Serving an agent loop means running tools for whoever can reach the socket,
so this mount is off unless a deployment turns it on, and "off" is the state
that requires nobody to do anything:

* it is **not** in :data:`.mounts.BUILTIN_MOUNT_FACTORIES`, so composing the
  app never reaches it;
* it is **not** declared as a ``service`` in any extension manifest, so
  discovery never reaches it either;
* and it cannot be built without a ``HeadlessChat``, which cannot itself be
  built without a turn deadline. Turning it on therefore names the bound in
  the same breath.

Turning it on is three lines in whatever wires the node::

    from axiom.extensions.builtins.chat.headless import HeadlessChat
    from axiom.extensions.builtins.http.agent_mount import agent_mount_spec
    from axiom.extensions.builtins.http.registry import register_router

    register_router(agent_mount_spec(chat=HeadlessChat(turn_deadline=30.0)))

Why an explicit call rather than a profile or an environment variable. A
``MountSpec`` gated by ``profiles`` is registered either way and merely
filtered at compose time, so a node run under the matching profile would get
an agent loop without anyone deciding to; and a gate that silently drops a
mount somebody did register is a quiet failure of exactly the kind this
serving path keeps being bitten by. An environment variable is a second way in
that no code review sees. One loud gate, in the deployment's own wiring, is
the whole mechanism.

It claims ``/agent`` rather than ``/v1``, so it can stand alongside the live
single-call gateway rather than displacing it. A client points its base URL at
``…/agent/v1``. Claiming ``/v1`` instead is a supported argument to the
factory and is what a cutover would eventually pass, at which point the
gateway mount has to come out of the built-in tuple, because the registry
refuses two mounts over one namespace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from axiom.serve.agent_backend import AgentChatBackend
from axiom.serve.chat_completions import (
    ChatCompletionError,
    ChatCompletionsHandler,
    render_sse,
)

from .registry import MountSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from axiom.extensions.builtins.chat.headless import HeadlessChat

#: Request fields the handler forwards to the backend, and therefore the ones
#: the streaming pre-flight has to check before a stream is opened.
_PREFLIGHT_FIELDS = ("tools", "tool_choice")


def build_agent_router(backend: AgentChatBackend, *, prefix: str = "/agent") -> APIRouter:
    """Build the OpenAI-compatible router served by an agent turn.

    One route, ``POST {prefix}/v1/chat/completions``, answering in a single
    response or as server-sent events when the request asks to stream. Every
    refusal the backend makes is rendered as the OpenAI 400 envelope, and the
    streaming path is pre-flighted so a refusal arrives as a status code
    rather than as an exception in the middle of an open stream.
    """
    router = APIRouter()
    handler = ChatCompletionsHandler(backend=backend)

    @router.post(f"{prefix}/v1/chat/completions")
    async def agent_chat_completions(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - any parse failure is one bad request
            return _bad_request("Invalid JSON")
        if not isinstance(payload, dict):
            return _bad_request("request body must be a JSON object")

        preflight = {k: payload[k] for k in _PREFLIGHT_FIELDS if k in payload}
        try:
            backend.validate_request(payload.get("messages"), **preflight)
        except ChatCompletionError as err:
            return JSONResponse(err.to_error_response(), status_code=err.status_code)

        try:
            if payload.get("stream"):
                return StreamingResponse(
                    render_sse(handler.handle_stream(payload)),
                    media_type="text/event-stream",
                )
            result = await run_in_threadpool(handler.handle, payload)
        except ChatCompletionError as err:
            # The pre-flight above should have caught this. Catching it here
            # too means a refusal the two ever disagree about still reaches
            # the caller as a bad request rather than as a server error.
            return JSONResponse(err.to_error_response(), status_code=err.status_code)
        return JSONResponse(result)

    return router


def agent_mount_spec(*, chat: HeadlessChat, prefix: str = "/agent") -> MountSpec:
    """``/agent``: the bounded, opt-in agent loop over the serving contract.

    Args:
        chat: The per-process headless agent. Its ``turn_deadline`` is the
            bound every request served here runs under.
        prefix: Namespace to claim. The default stands alongside the live
            single-call gateway on ``/v1``.

    Raises:
        TypeError: when ``chat`` is not a chat handle.
        ValueError: when ``chat`` carries no positive ``turn_deadline``.
    """
    backend = AgentChatBackend(chat)
    return MountSpec(
        prefix=prefix,
        router=build_agent_router(backend, prefix=prefix),
        extension="chat",
        # Tool execution on behalf of a caller stays on the loopback face
        # unless a deployment deliberately says otherwise.
        bind="127.0.0.1",
        trust_zone="loopback",
    )


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": None,
            }
        },
    )


__all__ = ["agent_mount_spec", "build_agent_router"]
