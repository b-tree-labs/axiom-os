# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Where the gateway puts prompt-cache markers on a request (#2210, ADR-272, RS-P6, RS-P10).

Interim, until the model registry (#1578) holds each model's cache facts: the
facts come from the provider entry (``cache_mode``, ``cache_min_tokens``) or
from the small table below. When the registry lands, the facts move there and
the placement stays.

On an entry whose model caches only where marked (``explicit``), a call that
expects reuse gets at most two markers: the end of the system part and the end
of the newest message (in a tool loop, the newest tool result). A call expects
reuse when it belongs to a session or carries history; a one-shot call does
not, and is never marked, because a write costs more than plain input. A
prefix below the model's minimum is not marked: the provider would not cache it.

Every marker buys the short (five-minute) lifetime. Choosing the long one from
the expected gap between calls is later work (RS-P10-A3).

Off unless the setting ``llm.cache.place_markers`` is on. A marker is only
worth its write when the system part is the same on the next call, which the
stable request layout (``chat.prompt.stable_layout``) provides; turn both on
together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SETTING = "llm.cache.place_markers"
EPHEMERAL = {"type": "ephemeral"}
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class CacheFacts:
    """How a model caches: ``none``, ``automatic`` (no markers needed) or ``explicit`` (only where marked)."""

    mode: str
    min_tokens: int


#: Models whose cache facts are known without a declaration, by name prefix (longest wins). Anthropic
#: caches only where marked; a prefix shorter than the minimum is not cached and not charged, so a
#: minimum set too low costs nothing and an entry may raise it with ``cache_min_tokens``.
_TABLE: dict[str, CacheFacts] = {
    "claude-": CacheFacts("explicit", 1024),
}


def enabled() -> bool:
    """``llm.cache.place_markers``; any settings trouble reads as off."""
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        value = SettingsStore().get(SETTING, False)
    except Exception:  # noqa: BLE001 - a marker is an optimisation, never a reason to fail a call
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def facts_of(provider: Any) -> CacheFacts | None:
    """The entry's own declaration first, then the table by model name. ``None`` when nobody declared it."""
    model = str(getattr(provider, "model", "") or "")
    known = next(
        (_TABLE[k] for k in sorted(_TABLE, key=len, reverse=True) if model.startswith(k)), None
    )
    mode = getattr(provider, "cache_mode", None) or (known.mode if known else None)
    if not mode:
        return None
    minimum = getattr(provider, "cache_min_tokens", None) or (known.min_tokens if known else 0)
    return CacheFacts(str(mode), int(minimum))


def _tokens(value: Any) -> int:
    if isinstance(value, str):
        return len(value) // _CHARS_PER_TOKEN
    if isinstance(value, list | tuple):
        return sum(_tokens(v) for v in value)
    if isinstance(value, dict):
        return sum(_tokens(v) for v in value.values())
    return 0


def place(
    provider: Any, system: str, messages: list[dict[str, Any]], *, session: str
) -> tuple[str, list[dict[str, Any]]]:
    """``(system, messages)`` with markers where they will be read; unchanged when none belong.

    A caller that placed its own markers (a harness through the bridge) is left alone: two placers
    would fight over the provider's limit of four.
    """
    from axiom.llm.gateway import CachedSystem, _count_cache_markers

    if not messages or not enabled():
        return system, messages
    facts = facts_of(provider)
    if facts is None or facts.mode != "explicit":
        return system, messages
    if _count_cache_markers(system, messages):
        return system, messages
    if not session and len(messages) < 2:
        return system, messages  # one-shot: nothing will read what this call would write

    system_tokens = _tokens(str(system)) if system else 0
    if system and system_tokens >= facts.min_tokens:
        system = CachedSystem(
            str(system), [{"type": "text", "text": str(system), "cache_control": EPHEMERAL}]
        )
    if system_tokens + _tokens(messages) >= facts.min_tokens:
        messages = [*messages[:-1], {**messages[-1], "cache_control": EPHEMERAL}]
    return system, messages
