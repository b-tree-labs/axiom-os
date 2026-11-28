# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Owner-nickname resolution for HERALD servant-naming (ADR-066 PR-2).

The possessive ``{owner}`` in "Ben's RIVET 0.6.0" is the human (or host)
that owns the agent, derived from the agent principal's ``:context``
(``@rivet:bens`` → context ``"bens"``).

Federation is inherent — every node *has* an identity by virtue of
existing; there is no init ceremony, and "solo" is just the empty-peer
case. So this resolver reads the inherent local identity and never
returns empty:

1. **remote peer's agent** (the context is a known peer) → peer registry
   ``display_name`` ("Alice's RIVET").
2. **local / own context** → ``settings user.name`` ("Ben's RIVET").
3. **else** → the **birth-host** captured once at first run and
   persisted ("ben-mbp's RIVET"), following the ``@local:<host>``
   convention in ``infra/tasks/cli.py``. This is the automatic default,
   not a fail-closed last resort.

One possessive render form is used across all three tiers (the caller
formats ``{owner}'s {agent} {version}``); this module only resolves the
``{owner}`` token.
"""

from __future__ import annotations

import socket
from typing import Any, Protocol

_BIRTH_HOST_KEY = "identity.birth_host"


class _SettingsLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


class _PeersLike(Protocol):
    def for_context(self, context: str) -> Any: ...


def _host_now() -> str:
    """Short hostname, matching the ``@local:<host>`` convention."""
    try:
        return socket.gethostname().split(".")[0] or "local"
    except Exception:
        return "local"


def birth_host(settings: _SettingsLike | None = None) -> str:
    """The host where this node was *born* — captured once, then stable.

    Read the persisted ``identity.birth_host``; if unset and a writable
    settings store is available, capture the current short hostname and
    persist it (global scope) so it survives later hostname changes.
    """
    if settings is not None:
        stored = settings.get(_BIRTH_HOST_KEY, "")
        if stored:
            return stored
    host = _host_now()
    # Best-effort persist so "born" stays stable; never let a settings
    # write failure break a notification render.
    if settings is not None and hasattr(settings, "set"):
        try:
            settings.set(_BIRTH_HOST_KEY, host, scope="global")  # type: ignore[attr-defined]
        except Exception:
            pass
    return host


def resolve_owner_display(
    context: str | None,
    *,
    local_context: str | None,
    settings: _SettingsLike | None = None,
    peers: _PeersLike | None = None,
    host: str | None = None,
) -> str:
    """Resolve the possessive owner token for an agent's ``:context``.

    ``context`` is the agent principal's context (``@rivet:bens`` → ``"bens"``).
    ``local_context`` is this node's own context. ``host`` overrides the
    birth-host (tests inject it); when ``None`` the persisted/derived
    birth-host is used. Never returns empty.
    """
    is_local = context is None or local_context is None or context == local_context

    # Tier 1 — remote peer.
    if not is_local and peers is not None and context is not None:
        peer = peers.for_context(context)
        if peer is not None and getattr(peer, "display_name", ""):
            return peer.display_name

    # Tier 2 — local owner via user.name.
    if is_local and settings is not None:
        name = settings.get("user.name", "") or ""
        if name.strip():
            return name.strip()

    # Tier 3 — birth-host (automatic, never empty).
    if host is not None and host.strip():
        return host.strip()
    return birth_host(settings)


__all__ = ["resolve_owner_display", "birth_host"]
