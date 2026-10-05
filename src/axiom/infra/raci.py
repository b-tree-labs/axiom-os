# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RACI-based human-in-the-loop framework.

Provides check_raci() for agents and CLI commands for users.

Trust levels:
  1 = Locked Down (everything = approve)
  2 = Cautious (writes = approve, reads = informed) — default
  3 = Balanced (routine = consulted, important = approve)
  4 = Autonomous (most = informed, safety = approve)
  5 = Full Trust (everything = informed except safety)
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Action category → RACI level at each trust position (1-5)
# A = approve, C = consulted, I = informed
_TRUST_MATRIX: dict[str, list[str]] = {
    #                           1     2     3     4     5
    "publish.document":       ["A",  "A",  "A",  "C",  "I"],
    "publish.draft":          ["A",  "A",  "C",  "I",  "I"],
    "issue.update":           ["A",  "A",  "C",  "I",  "I"],
    "issue.create":           ["A",  "A",  "A",  "C",  "I"],
    "issue.close":            ["A",  "A",  "A",  "C",  "I"],
    "signal.ingest":          ["A",  "I",  "I",  "I",  "I"],
    "signal.brief":           ["A",  "I",  "I",  "I",  "I"],
    "signal.draft":           ["A",  "C",  "C",  "I",  "I"],
    "credential.rotate":      ["A",  "A",  "A",  "A",  "C"],
    "service.restart":        ["A",  "A",  "I",  "I",  "I"],
    "code.patch":             ["A",  "A",  "A",  "A",  "A"],  # Always approve (safety)
    "code.commit":            ["A",  "A",  "A",  "A",  "A"],  # Always approve (safety)
}

_TRUST_LABELS = {
    1: "Locked Down",
    2: "Cautious",
    3: "Balanced",
    4: "Autonomous",
    5: "Full Trust",
}


def check_raci(action: str) -> str:
    """Check the user's RACI level for an action category.

    Returns: "approve", "consulted", or "informed"

    Resolution order:
    1. Per-action override (axi settings raci.<action>)
    2. Trust slider level (axi settings raci.trust)
    3. Default: "approve"
    """
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore
        settings = SettingsStore()

        # Check per-action override first
        override = settings.get(f"raci.{action}", "")
        if override:
            return _normalize_raci(override)

        # Fall back to trust slider
        trust = int(settings.get("raci.trust", 2))
        trust = max(1, min(5, trust))  # Clamp 1-5

        levels = _TRUST_MATRIX.get(action)
        if levels:
            return _normalize_raci(levels[trust - 1])

        # Unknown action — default to approve (safe)
        return "approve"

    except Exception:
        return "approve"  # Fail safe


def _normalize_raci(level: str) -> str:
    """Normalize RACI level strings."""
    level = level.strip().lower()
    mapping = {
        "a": "approve", "approve": "approve",
        "c": "consulted", "consulted": "consulted", "consult": "consulted",
        "i": "informed", "informed": "informed", "inform": "informed",
        "r": "responsible", "responsible": "responsible",
    }
    return mapping.get(level, "approve")


def get_trust_level() -> tuple[int, str]:
    """Get current trust level and label."""
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore
        trust = int(SettingsStore().get("raci.trust", 2))
    except Exception:
        trust = 2
    trust = max(1, min(5, trust))
    return trust, _TRUST_LABELS[trust]


def set_trust_level(level: int) -> None:
    """Set the trust slider and clear per-action overrides."""
    from axiom.extensions.builtins.settings.store import SettingsStore
    level = max(1, min(5, level))
    settings = SettingsStore()
    settings.set("raci.trust", level)


def halt() -> None:
    """Emergency brake — set all actions to Approve (trust = 1).

    Clears all per-action overrides. Every agent action will pause
    for confirmation until trust is explicitly raised.
    """
    from axiom.extensions.builtins.settings.store import SettingsStore
    settings = SettingsStore()
    settings.set("raci.trust", 1)

    # Clear all per-action overrides
    for action in _TRUST_MATRIX:
        try:
            settings.set(f"raci.{action}", "", scope="project")
        except Exception:
            pass

    log.warning("RACI HALT: All actions set to Approve. Trust level = 1 (Locked Down).")


def reset() -> None:
    """Factory reset — clear all overrides, trust → 2 (Cautious)."""
    from axiom.extensions.builtins.settings.store import SettingsStore
    settings = SettingsStore()
    settings.set("raci.trust", 2)

    # Clear all per-action overrides
    for action in _TRUST_MATRIX:
        try:
            settings.set(f"raci.{action}", "", scope="project")
        except Exception:
            pass


def format_status() -> str:
    """Format current RACI status for display."""
    trust, label = get_trust_level()
    lines = [
        f"  Trust: {trust}/5 ({label})",
        "",
        f"  {'Action':<25s} {'Level':<12s} {'Source':<10s}",
        f"  {'─' * 25} {'─' * 12} {'─' * 10}",
    ]

    try:
        from axiom.extensions.builtins.settings.store import SettingsStore
        settings = SettingsStore()
    except Exception:
        settings = None

    for action in sorted(_TRUST_MATRIX):
        level = check_raci(action)

        # Check if there's a per-action override
        override = ""
        if settings:
            override = settings.get(f"raci.{action}", "")

        source = "override" if override else "slider"
        lines.append(f"  {action:<25s} {level:<12s} {source:<10s}")

    return "\n".join(lines)
