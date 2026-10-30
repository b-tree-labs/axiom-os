# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A laptop bound to a site node asks that node's model.

A bound install already reads its data and its documents from the node (``read_target`` and the
remote document store). Chat did not follow: it used only this machine's providers, so with none
configured a question got "LLM extraction unavailable", and with a stray server on localhost it got
an answer from a model that held none of the site's context.

The node serves an OpenAI-compatible ``/v1`` behind the same gate as its data reads, so the binding
already says everything chat needs: where (the binding's face URL) and with which key (the binding's
own ``token_ref``, resolved by :func:`read_target.bound_key_for`, which never offers it to any other
host). Data, documents and the model then all come from one place.

It is a default, and the person's own choice wins: an explicitly configured provider that can be
called, ``--provider``, and ``--local`` all keep their meaning. A server found by probing localhost
is not a choice anybody made, so a bound install sets it aside rather than letting it answer.

Never raises: a binding that cannot be read is no binding.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Seconds to wait for the node to say which model it serves. Short: this runs when chat starts, and
#: a node that cannot answer this quickly is reported by the first turn, plainly.
MODEL_PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class BoundModel:
    """The bound node's model: where it is and what it is called."""

    site: str
    host: str
    endpoint: str
    model: str
    #: Why the model's name could not be read from the node, or empty when it was.
    probe_error: str = ""


#: What this process's chat is answering with, for the banner (set by :func:`apply_bound_model`).
_CHOSEN: dict[str, str] = {}


def chosen_model() -> dict[str, str]:
    """``{"model", "where"}`` of the provider chat starts with, or empty before one is chosen."""
    return dict(_CHOSEN)


def _remember(provider: Any | None) -> None:
    _CHOSEN.clear()
    if provider is None:
        return
    try:
        from axiom.llm.placement import placement_of

        placement = placement_of(provider)
        where = "this machine" if placement.placement == "local" else (placement.host or "")
    except Exception:  # noqa: BLE001 - a banner detail, never a failure
        where = ""
    _CHOSEN.update({"model": str(getattr(provider, "model", "") or ""), "where": where})


def explicitly_configured(gateway: Any) -> bool:
    """True when a provider somebody configured, and that can be called, is on the gateway."""
    found = getattr(gateway, "auto_discovered", set()) or set()
    for provider in getattr(gateway, "providers", []) or []:
        if provider.name in found:
            continue
        try:
            if provider.is_usable:
                return True
        except Exception:  # noqa: BLE001 - a provider that cannot say is not a choice
            continue
    return False


#: The context window a node reported for its model, by face URL, when it reported one.
_WINDOWS: dict[str, int] = {}

#: Where OpenAI-compatible servers put a model's context length in ``/v1/models``.
_WINDOW_KEYS = ("context_window", "max_model_len", "context_length", "max_context_length")


def _window_of(item: dict) -> int:
    for key in _WINDOW_KEYS:
        try:
            value = int(item.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _node_model(face_url: str, key: str, *, timeout: float = MODEL_PROBE_TIMEOUT) -> tuple[str, str]:
    """``(model id, error)`` from the node's ``GET /v1/models``. Sends the key to that node only."""
    try:
        import requests

        headers = {"authorization": f"Bearer {key}"} if key else {}
        response = requests.get(f"{face_url}/v1/models", headers=headers, timeout=timeout)
        if response.status_code >= 400:
            return "", f"the node answered HTTP {response.status_code} to /v1/models"
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - said plainly in the banner, never raised
        return "", f"the node did not say which model it serves ({type(exc).__name__})"
    items = body.get("data") if isinstance(body, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                window = _window_of(item)
                if window:
                    _WINDOWS[face_url] = window
                return str(item["id"]), ""
    return "", "the node listed no model at /v1/models"


def resolve_bound_model() -> tuple[BoundModel, str] | None:
    """The bound node's model and the key to reach it, or None when this install is not bound.

    Resolved the way a data read resolves its target: an install that holds the medallion is the
    node, not a laptop bound to one, so it is never sent anywhere.
    """
    try:
        from axiom.extensions.builtins.data_platform.read_target import (
            bound_key_for,
            resolve_read_target,
        )

        target = resolve_read_target()
        if target.where != "binding" or not target.face_url:
            return None
        key = bound_key_for(target.face_url)
    except Exception as exc:  # noqa: BLE001 - an unreadable binding is no binding
        log.debug("chat: binding not readable: %s", exc)
        return None
    host = urlparse(target.face_url).hostname or target.face_url
    model, error = _node_model(target.face_url, key)
    if not key:
        error = (
            "no key: the binding's token_ref did not resolve on this machine "
            "(check the vault settings)"
        )
    bound = BoundModel(
        site=target.site,
        host=host,
        endpoint=f"{target.face_url}/v1",
        model=model or "default",
        probe_error=error,
    )
    return bound, key


def apply_bound_model(gateway: Any, *, local: bool = False) -> BoundModel | None:
    """Make the bound node's model chat's provider, unless the person chose another. Never raises.

    Returns the bound model when it was applied, else None. Either way the provider chat starts
    with is remembered for the banner (:func:`chosen_model`).
    """
    applied: BoundModel | None = None
    try:
        if not (local or getattr(gateway, "_provider_override", None) or explicitly_configured(gateway)):
            resolved = resolve_bound_model()
            if resolved is not None:
                bound, key = resolved
                applied = bound
                _install(gateway, bound, key)
    except Exception as exc:  # noqa: BLE001 - chat still starts on what it has
        log.warning("chat: the bound node's model could not be offered: %s", exc)
        applied = None
    try:
        _remember(gateway.preview_provider("any"))
    except Exception:  # noqa: BLE001
        _remember(None)
    return applied


def choose_default_model(gateway: Any, *, local: bool = False) -> None:
    """The bound node's model when this install is bound, else the product's default. Never raises.

    The one resolution every chat surface makes before it answers or reports: ``--ask``, the REPL,
    and ``--where``/``/where`` through a headless agent. ``--where`` used to build a bare gateway and
    report whatever this machine had (a local model, "BOUND: no model") while ``--ask`` answered on
    the node's model. Either is a default: ``--provider``, ``--local`` and a configured provider
    that can be called keep their meaning (see :func:`apply_bound_model` and ``product_default``).
    """
    try:
        if apply_bound_model(gateway, local=local) is None:
            from .product_default import apply_product_default

            apply_product_default(gateway, local=local)
            _remember(gateway.preview_provider("any"))
    except Exception as exc:  # noqa: BLE001 - chat still starts on what it has
        log.debug("chat: the default model could not be chosen: %s", exc)


def _install(gateway: Any, bound: BoundModel, key: str) -> None:
    from axiom.llm.gateway import LLMProvider

    found = set(getattr(gateway, "auto_discovered", set()) or set())
    if found:
        # A probed localhost server is not the person's choice and holds none of the site's
        # context. Left in, it is the fallback when the node refuses, and its answer reads as real.
        gateway.providers = [p for p in gateway.providers if p.name not in found]
        gateway.auto_discovered = set()
    provider = LLMProvider(
        name=f"{bound.site} node",
        uid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"bound-node#{bound.endpoint}")),
        endpoint=bound.endpoint,
        model=bound.model,
        api_key_env="",
        priority=0,
        use_for=["chat", "fallback"],
        routing_tier="any",
        placement="remote-node",
        node_host=bound.host,
        # Known only when the node says (``max_model_len`` and the like); chat then chooses the
        # tools that fit (``bound_tools.fit_to_context``) rather than send more than it holds.
        context_window=_WINDOWS.get(bound.endpoint[: -len("/v1")], 0),
    )
    # The binding's key, resolved once here; never looked up under any other name.
    object.__setattr__(provider, "_vault_checked", True)
    object.__setattr__(provider, "_vault_key", key or None)
    gateway.add_provider(provider, first=True)


__all__ = [
    "BoundModel",
    "MODEL_PROBE_TIMEOUT",
    "apply_bound_model",
    "choose_default_model",
    "chosen_model",
    "explicitly_configured",
    "resolve_bound_model",
]
