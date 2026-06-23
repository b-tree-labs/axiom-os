# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tool-dispatch gateway with platform-hook wiring.

Wraps the actual tool dispatcher (chat tool registry) with the
``tool.pre_invoke`` interceptor and ``tool.post_invoke`` observer events.
This is the single call-site spec §8a points to.

Call-site contract:

    from axiom.infra.tool_gateway import dispatch_tool

    result = dispatch_tool(
        tool_name="search",
        args={"query": "..."},
        principal="@alice:axiom",
        hookbus=hookbus,
        eventbus=eventbus,
    )

Hook semantics:

- ``tool.pre_invoke`` fires first. ``deny()`` raises `HookDenied`,
  ``request_approval()`` raises `ApprovalRequired`, ``allow_modified``
  splices into ``args`` before dispatch.
- The actual tool runs.
- ``tool.post_invoke`` fires with ``{tool_name, args, result, error,
  latency_ms, principal, tokens}``. ``tokens`` and ``latency_ms`` are
  populated from the dispatch wrapper; downstream consumers (cost meter,
  audit log) get them for free.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from axiom.infra.bus import EventBus
from axiom.infra.hooks import (
    ApprovalRequired,
    HookBus,
    HookDenied,
    get_default_hookbus,
)

log = logging.getLogger("axiom.infra.tool_gateway")


def _default_dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Real dispatcher — falls back to chat tools when no override is given."""
    from axiom.extensions.builtins.chat.tools import execute_tool as _exec

    return _exec(name, args)


def _approx_tokens(payload: Any) -> int:
    """Cheap token count for telemetry — `cl100k`-ish ~4 chars/token.

    Avoids importing tiktoken in the hot path; downstream observers that
    need accurate counts can recompute from `args` / `result`.
    """
    text = str(payload) if payload is not None else ""
    return max(1, len(text) // 4)


def dispatch_tool(
    *,
    tool_name: str,
    args: dict[str, Any],
    principal: str,
    hookbus: HookBus | None = None,
    eventbus: EventBus | None = None,
    dispatcher: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    classification: str = "",
    ext_origin: str = "",
    model_id: str = "",
) -> dict[str, Any]:
    """Dispatch a tool call through the platform-hook chain.

    Args:
        tool_name: Registered tool name.
        args: Tool arguments. Mutable view spliced by `allow_modified`.
        principal: Caller principal in ``@name:context`` form.
        hookbus: Override the default `HookBus`. Production wiring should
            pass an explicit instance; tests may rely on the default.
        eventbus: Where ``tool.post_invoke`` is published. None disables
            the post event entirely (useful for tests that don't care).
        dispatcher: Override the actual tool runner. Defaults to
            `axiom.extensions.builtins.chat.tools.execute_tool`.
        classification: Optional classification stamp for the payload.
        ext_origin: Originating extension name; populated by callers that
            know which extension registered the tool.
        model_id: Optional model id for telemetry.

    Returns:
        The tool's raw result dict.

    Raises:
        HookDenied: a hook returned `deny()`.
        ApprovalRequired: a hook returned `request_approval()`.
    """
    hb = hookbus if hookbus is not None else get_default_hookbus()
    disp = dispatcher if dispatcher is not None else _default_dispatcher

    pre_payload: dict[str, Any] = {
        "tool_name": tool_name,
        "args": dict(args),
        "principal": principal,
        "classification": classification,
        "ext_origin": ext_origin,
    }

    pre_result = hb.fire("tool.pre_invoke", pre_payload, principal)

    if pre_result.decision == "deny":
        raise HookDenied(pre_result.reason, hook_source=ext_origin)
    if pre_result.decision == "approval_required":
        raise ApprovalRequired(
            pre_result.reason,
            hook_source=ext_origin,
            token=pre_result.approval_token,
        )

    effective_args = dict(args)
    if pre_result.decision == "modify" and pre_result.modified_payload:
        if "args" in pre_result.modified_payload:
            effective_args = dict(pre_result.modified_payload["args"])

    started = time.monotonic()
    error_msg = ""
    result: dict[str, Any]
    try:
        result = disp(tool_name, effective_args)
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        result = {"error": error_msg}
        # Still fire the post event so observers see the failure.
        latency_ms = int((time.monotonic() - started) * 1000)
        if eventbus is not None:
            try:
                eventbus.publish(
                    "tool.post_invoke",
                    {
                        "tool_name": tool_name,
                        "args": effective_args,
                        "result": None,
                        "error": error_msg,
                        "latency_ms": latency_ms,
                        "principal": principal,
                        "model_id": model_id,
                        "tokens": _approx_tokens(effective_args),
                    },
                    source="tool_gateway",
                )
            except Exception as pub_exc:  # noqa: BLE001
                log.debug("post_invoke publish failed: %s", pub_exc)
        raise

    latency_ms = int((time.monotonic() - started) * 1000)

    if eventbus is not None:
        try:
            eventbus.publish(
                "tool.post_invoke",
                {
                    "tool_name": tool_name,
                    "args": effective_args,
                    "result": result,
                    "error": "",
                    "latency_ms": latency_ms,
                    "principal": principal,
                    "model_id": model_id,
                    "tokens": _approx_tokens(effective_args) + _approx_tokens(result),
                },
                source="tool_gateway",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("post_invoke publish failed: %s", exc)

    return result


__all__ = ["dispatch_tool"]
