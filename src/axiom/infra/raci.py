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
# A = approve, C = consulted, N = act-then-notify, I = informed
# Landing rows are the ADR-045 review ladder. `code.patch` and
# `code.commit` stay approve at every level: that safety floor is unchanged.
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
    # Pull-request landing. Strict (1–2) approves. Balanced (3) consults.
    # Autonomous (4) act-then-notifies reversible landings; batch stays
    # consulted. Full trust (5) informs only the trivial reversible ones.
    "pr.merge":               ["A",  "A",  "C",  "N",  "N"],
    "pr.repair":              ["A",  "A",  "C",  "N",  "I"],
    "pr.close":               ["A",  "A",  "C",  "N",  "I"],
    "pr.rerun":               ["A",  "A",  "C",  "N",  "I"],
    "pr.update-branch":       ["A",  "A",  "C",  "N",  "I"],
    "pr.batch":               ["A",  "A",  "A",  "C",  "N"],
    "pr.enqueue":             ["A",  "A",  "C",  "N",  "N"],
}

#: Presets name a trust band. The shipped preset is autonomous (4–5).
#: The operating point is the low end of the band, except strict, which
#: sits at the locked end so an opt-out is the tighter stop.
PRESET_BANDS: dict[str, tuple[int, int]] = {
    "strict": (1, 2),
    "balanced": (3, 3),
    "autonomous": (4, 5),
}
SHIPPED_PRESET = "autonomous"
_LADDER = ("A", "C", "N", "I")

_TRUST_LABELS = {
    1: "Locked Down",
    2: "Cautious",
    3: "Balanced",
    4: "Autonomous",
    5: "Full Trust",
}


def canonical_action(action: str) -> str:
    """``merge`` and ``update_branch`` become ``pr.merge`` and ``pr.update-branch``."""
    name = (action or "").strip().lower().replace("_", "-")
    if not name:
        return "pr.merge"
    if name.startswith("pr."):
        return name
    return f"pr.{name}"


def operating_trust(preset: str, explicit: int | None = None) -> int:
    """Trust level the preset selects.

    An explicit trust inside the preset's band is honored. A trust outside
    the band leaves the preset's operating point in place, so the legacy
    default of 2 does not cancel the autonomous preset.
    """
    band = PRESET_BANDS.get(preset, PRESET_BANDS[SHIPPED_PRESET])
    lo, hi = band
    if explicit is not None:
        try:
            chosen = int(explicit)
        except (TypeError, ValueError):
            chosen = lo
        else:
            if lo <= chosen <= hi:
                return chosen
    return lo


def matrix_tier(action: str, trust: int) -> str:
    """The letter in the trust matrix: ``A``, ``C``, ``N``, or ``I``.

    An unknown action is ``A``. Trust is clamped to 1–5.
    """
    levels = _TRUST_MATRIX.get(canonical_action(action))
    if levels is None:
        levels = _TRUST_MATRIX.get(action)
    if not levels:
        return "A"
    trust = max(1, min(5, int(trust)))
    cell = levels[trust - 1]
    return cell if cell in _LADDER else "A"


def toward_approve(tier: str, steps: int = 1) -> str:
    """Move ``steps`` places toward ``A`` on the ladder ``A → C → N → I``."""
    if tier not in _LADDER:
        return "A"
    index = _LADDER.index(tier)
    return _LADDER[max(0, index - max(0, steps))]


def check_raci(action: str) -> str:
    """Check the user's RACI level for an action category.

    Returns: "approve", "consulted", "notify", or "informed"

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
        "n": "notify", "notify": "notify", "act-then-notify": "notify",
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
