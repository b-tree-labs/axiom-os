# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Calendar provider factory — a vendor registry + detect().

Vendors register a builder ``(config) -> CalendarProvider``; the factory hands
back the right provider for a vendor name. ``detect`` reports connector state
per ADR-068 from a live ``health()`` probe, so onboarding can show an honest
status without the caller knowing vendor internals.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from axiom.extensions.builtins.schedule.calendar.protocol import CalendarProvider

_BUILDERS: dict[str, Callable[[dict], CalendarProvider]] = {}


def register_vendor(name: str, builder: Callable[[dict], CalendarProvider]) -> None:
    """Register a vendor builder. Called by each vendor module on import."""
    _BUILDERS[name] = builder


def available_vendors() -> list[str]:
    return sorted(_BUILDERS)


def get_provider(vendor: str, config: Optional[dict] = None) -> CalendarProvider:
    """Build the provider for ``vendor``. Raises KeyError for an unknown vendor."""
    if vendor not in _BUILDERS:
        raise KeyError(
            f"unknown calendar vendor {vendor!r}; available: {available_vendors()}"
        )
    return _BUILDERS[vendor](config or {})


def detect(vendor: str, config: Optional[dict] = None) -> dict[str, Any]:
    """Report connector state (ADR-068) for a vendor from a health probe.

    Returns ``{state, summary, vendor}`` where state is one of
    ``configured | broken | absent``. Wires to ``connector.detect`` for the full
    secrets/last-outcome derivation once the calendar connector lands.
    """
    if vendor not in _BUILDERS:
        return {"state": "absent", "summary": f"no {vendor} provider", "vendor": vendor}
    try:
        provider = _BUILDERS[vendor](config or {})
        ok = provider.health()
    except Exception as exc:  # noqa: BLE001 — detection must not raise
        return {"state": "broken", "summary": f"{vendor}: {exc!r}", "vendor": vendor}
    return {
        "state": "configured" if ok else "broken",
        "summary": f"{vendor}: {'healthy' if ok else 'unhealthy'}",
        "vendor": vendor,
    }


__all__ = ["available_vendors", "detect", "get_provider", "register_vendor"]
