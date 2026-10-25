# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Resolve a live channel from its connector descriptor (ADR-074, B1).

This is the generalization seam: it makes ``ConnectorDescriptor.provider_entry``
load-bearing. Given an enabled ``channel_adapter`` descriptor + its connection
creds, it imports the descriptor's factory and produces an ``InteractiveChannel``
— so every vendor (Slack today; SMS/email next) is reachable through one path
and the workflow above never names a vendor.

The factory convention: ``provider_entry = "module:make_<vendor>_channel"`` and
``make_<vendor>_channel(*, env: dict[str, str]) -> InteractiveChannel``. The
resolver hands the factory the descriptor's declared env (secrets resolved from
the keystore, non-secrets from the connection/env); the factory maps them to its
constructor. The result must satisfy the (``@runtime_checkable``)
``InteractiveChannel`` protocol or resolution fails loudly.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any

from axiom.extensions.builtins.notifications.channels.interactive import InteractiveChannel

# kind → the interface a provider of that kind must satisfy (ADR-074 axis 1).
_KIND_INTERFACE = {"channel_adapter": InteractiveChannel}


def _import_entry(entry: str) -> Callable[..., Any]:
    """Resolve a ``"module:attr"`` provider_entry to its factory callable."""
    if ":" not in entry:
        raise ValueError(f"provider_entry must be 'module:attr', got {entry!r}")
    mod_name, attr = entry.split(":", 1)
    return getattr(importlib.import_module(mod_name), attr)


def _default_resolve_secret(secret_ref: str | None, key: str) -> str | None:
    """Default secret resolution. Env-backed floor; keystore providers
    (``kubernetes://…`` per A2) layer in behind the same call later."""
    return os.environ.get(key)


def resolve_channel(
    descriptor: Any,
    connection: Any = None,
    *,
    resolve_secret: Callable[[str | None, str], str | None] = _default_resolve_secret,
    env_overrides: dict[str, str] | None = None,
    import_entry: Callable[[str], Callable[..., Any]] = _import_entry,
) -> InteractiveChannel:
    """Build the live ``InteractiveChannel`` a descriptor declares.

    ``connection`` supplies the keystore ``secret_ref`` for secret env vars.
    ``env_overrides`` wins over both (tests / explicit creds). ``import_entry``
    and ``resolve_secret`` are injectable for testing.
    """
    interface = _KIND_INTERFACE.get(getattr(descriptor, "kind", None))
    if interface is None:
        raise ValueError(
            f"resolve_channel only handles kinds {sorted(_KIND_INTERFACE)}; "
            f"got kind={getattr(descriptor, 'kind', None)!r}"
        )
    entry = getattr(descriptor, "provider_entry", None)
    if not entry:
        raise ValueError(f"{getattr(descriptor, 'name', '?')} has no provider_entry to resolve")

    secret_ref = getattr(connection, "secret_ref", None)
    env: dict[str, str | None] = {}
    for ev in getattr(descriptor, "env", []) or []:
        if env_overrides and ev.name in env_overrides:
            env[ev.name] = env_overrides[ev.name]
        elif getattr(ev, "is_secret", False):
            env[ev.name] = resolve_secret(secret_ref, ev.name)
        else:
            env[ev.name] = os.environ.get(ev.name)

    factory = import_entry(entry)
    channel = factory(env=env)
    if not isinstance(channel, interface):
        raise TypeError(
            f"provider_entry {entry!r} produced {type(channel).__name__}, "
            f"which does not satisfy {interface.__name__}"
        )
    return channel


__all__ = ["resolve_channel"]
