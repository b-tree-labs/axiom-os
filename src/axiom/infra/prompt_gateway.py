# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prompt-submit gateway with platform-hook wiring.

Wraps the model-call site (after the prompt composer renders, before the
gateway transports to the provider) with the ``prompt.pre_submit``
interceptor and ``prompt.post_submit`` observer events.

This is the call-site spec §8b points to. Production callers pass the
already-composed messages + system layers in; the gateway fires
``prompt.pre_submit``, splices any modifications, calls the provided
``transport`` callable, then fires ``prompt.post_submit``.

Decoupling the call-site from the LLM gateway keeps this module free of
provider-specific shape dependencies — `transport` is any callable that
takes the same kwargs and returns the response dict.
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

log = logging.getLogger("axiom.infra.prompt_gateway")


def _approx_tokens(payload: Any) -> int:
    """Cheap token estimate ~4 chars/token for telemetry."""
    text = str(payload) if payload is not None else ""
    return max(1, len(text) // 4)


def submit_prompt(
    *,
    messages: list[dict[str, Any]],
    system_layers: list[dict[str, Any]],
    principal: str,
    model_id: str,
    transport: Callable[..., dict[str, Any]],
    hookbus: HookBus | None = None,
    eventbus: EventBus | None = None,
    classification: str = "",
    cost_calculator: Callable[[dict[str, Any]], float] | None = None,
) -> dict[str, Any]:
    """Submit a composed prompt through the platform-hook chain.

    Args:
        messages: Conversation messages in API format.
        system_layers: Layered system contributions (composer's
            `debug()` output is the canonical shape; callers are free
            to pass any list of dicts).
        principal: Caller principal in ``@name:context`` form.
        model_id: Target model identifier.
        transport: Callable invoked with the (possibly modified) messages
            + system_layers + model_id. Must return a dict response.
        hookbus: Override the default `HookBus`.
        eventbus: Where ``prompt.post_submit`` is published. None disables.
        classification: Optional classification stamp.
        cost_calculator: Optional callable that produces a `cost_usd`
            value from the response. Default 0.0.

    Returns:
        The transport's response dict.

    Raises:
        HookDenied: a hook returned `deny()`.
        ApprovalRequired: a hook returned `request_approval()`.
    """
    hb = hookbus if hookbus is not None else get_default_hookbus()

    pre_payload: dict[str, Any] = {
        "messages": list(messages),
        "system_layers": list(system_layers),
        "principal": principal,
        "model_id": model_id,
        "classification": classification,
    }

    pre_result = hb.fire("prompt.pre_submit", pre_payload, principal)
    if pre_result.decision == "deny":
        raise HookDenied(pre_result.reason)
    if pre_result.decision == "approval_required":
        raise ApprovalRequired(
            pre_result.reason,
            token=pre_result.approval_token,
        )

    effective_messages = list(messages)
    effective_layers = list(system_layers)
    if pre_result.decision == "modify" and pre_result.modified_payload:
        if "messages" in pre_result.modified_payload:
            effective_messages = list(pre_result.modified_payload["messages"])
        if "system_layers" in pre_result.modified_payload:
            effective_layers = list(pre_result.modified_payload["system_layers"])

    started = time.monotonic()
    response = transport(
        messages=effective_messages,
        system_layers=effective_layers,
        model_id=model_id,
        principal=principal,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    cost_usd = cost_calculator(response) if cost_calculator else 0.0
    if eventbus is not None:
        try:
            eventbus.publish(
                "prompt.post_submit",
                {
                    "messages": effective_messages,
                    "response": response,
                    "latency_ms": latency_ms,
                    "principal": principal,
                    "model_id": model_id,
                    "tokens": _approx_tokens(effective_messages) + _approx_tokens(response),
                    "cost_usd": cost_usd,
                },
                source="prompt_gateway",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("post_submit publish failed: %s", exc)

    return response


__all__ = ["submit_prompt"]
