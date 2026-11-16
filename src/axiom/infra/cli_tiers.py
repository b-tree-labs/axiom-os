# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Progressive disclosure — reveal CLI commands based on user journey stage.

Commands are always available (any command can be run at any time).
This only controls what appears in --help output, so new users aren't
overwhelmed by 130+ commands on day 1.

Tiers:
  0 — Day 1: core workflow (init, validate, add, list, show, pull, materials)
  1 — Week 1: productivity (clone, diff, export, lineage, generate, lint)
  2 — Month 1: collaboration (sweep, share, receive, audit, federation, nodes)
  3 — Power user: advanced (research, knowledge, security, facility publish)
  4 — Operator: admin (chaos, federation leave, facility sync)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Tier definitions
TIER_NAMES = {
    0: "Getting Started",
    1: "Productivity",
    2: "Collaboration",
    3: "Advanced",
    4: "Administration",
}

# Default tier assignments for known commands
# Format: "noun:verb" -> tier
DEFAULT_TIERS: dict[str, int] = {
    # Tier 0 — Day 1
    "model:init": 0, "model:validate": 0, "model:add": 0,
    "model:list": 0, "model:show": 0, "model:pull": 0,
    "model:materials": 0, "model:search": 0,
    "facility:list": 0, "facility:install": 0, "facility:show": 0,
    "status:": 0, "update:": 0,

    # Tier 1 — Week 1
    "model:clone": 1, "model:diff": 1, "model:export": 1,
    "model:lineage": 1, "model:generate": 1, "model:lint": 1,
    "facility:materials": 1, "facility:uninstall": 1,
    "agents:status": 1, "db:status": 1, "connect:": 1,

    # Tier 2 — Month 1
    "model:sweep": 2, "model:share": 2, "model:receive": 2,
    "model:audit": 2,
    "federation:status": 2, "federation:init": 2,
    "federation:peers": 2, "federation:resources": 2,
    "nodes:add": 2, "nodes:list": 2, "nodes:status": 2,
    "knowledge:status": 2,

    # Tier 3 — Power user
    "research:create": 3, "research:list": 3, "research:show": 3,
    "research:claim": 3, "research:submit": 3, "research:publish": 3,
    "research:chain": 3,
    "knowledge:velocity": 3, "knowledge:accumulation": 3,
    "knowledge:impact": 3, "knowledge:report": 3, "knowledge:gaps": 3,
    "security:status": 3, "security:alerts": 3, "security:trust": 3,
    "federation:invite": 3, "federation:join": 3,
    "facility:init": 3, "facility:publish": 3,
    "nodes:upgrade": 3, "nodes:remove": 3,

    # Tier 4 — Operator
    "chaos:list": 4, "chaos:run": 4, "chaos:status": 4,
    "security:scan": 4, "security:rules": 4, "security:escalation": 4,
    "security:resolve": 4,
    "federation:leave": 4,
    "facility:sync": 4,
    "release:": 4,
}


@dataclass
class UserProfile:
    """Tracks the user's journey stage for progressive disclosure."""
    tier: int = 0
    actions_completed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"tier": self.tier, "actions_completed": self.actions_completed}


def _profile_path() -> Path:
    return Path.home() / ".axi" / "profile.json"


def get_user_tier() -> int:
    """Get the current user's disclosure tier."""
    path = _profile_path()
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("tier", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def set_user_tier(tier: int) -> None:
    """Manually set the user's disclosure tier."""
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = _load_profile()
    profile.tier = max(0, min(4, tier))
    path.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")


def record_action(noun: str, verb: str) -> None:
    """Record that the user performed an action (may auto-advance tier)."""
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = _load_profile()

    action = f"{noun}:{verb}"
    if action not in profile.actions_completed:
        profile.actions_completed.append(action)

    # Auto-advance tier based on actions
    profile.tier = _compute_tier(profile.actions_completed)

    path.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")


def _compute_tier(actions: list[str]) -> int:
    """Compute tier from completed actions."""
    tier = 0

    # Tier 0 → 1: user has added at least one model
    if any(a.startswith("model:add") for a in actions):
        tier = max(tier, 1)

    # Tier 1 → 2: user has connected to federation or used collaboration features
    if any(a.startswith(("federation:", "nodes:", "model:share", "model:sweep")) for a in actions):
        tier = max(tier, 2)

    # Tier 2 → 3: user has used research or advanced features
    if any(a.startswith(("research:", "knowledge:", "security:")) for a in actions):
        tier = max(tier, 3)

    # Tier 3 → 4: user has used admin features
    if any(a.startswith(("chaos:", "release:", "federation:leave")) for a in actions):
        tier = max(tier, 4)

    return tier


def get_command_tier(noun: str, verb: str = "") -> int:
    """Get the tier for a specific command."""
    key = f"{noun}:{verb}"
    if key in DEFAULT_TIERS:
        return DEFAULT_TIERS[key]
    # Try noun-level default
    noun_key = f"{noun}:"
    if noun_key in DEFAULT_TIERS:
        return DEFAULT_TIERS[noun_key]
    return 0  # unknown commands default to tier 0 (always visible)


def should_show_command(noun: str, verb: str = "", user_tier: int | None = None) -> bool:
    """Should this command appear in --help for the current user?"""
    if user_tier is None:
        user_tier = get_user_tier()
    cmd_tier = get_command_tier(noun, verb)
    return cmd_tier <= user_tier


def filter_subparsers_for_help(noun: str, subparsers: dict, user_tier: int | None = None) -> dict:
    """Filter a dict of subparser name -> parser for help display."""
    if user_tier is None:
        user_tier = get_user_tier()
    return {
        name: parser for name, parser in subparsers.items()
        if should_show_command(noun, name, user_tier)
    }


def _load_profile() -> UserProfile:
    path = _profile_path()
    if not path.exists():
        return UserProfile()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UserProfile(
            tier=data.get("tier", 0),
            actions_completed=data.get("actions_completed", []),
        )
    except (json.JSONDecodeError, OSError):
        return UserProfile()
