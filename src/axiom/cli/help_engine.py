# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Role + intent + tier help engine for `axi`.

Implements the incremental-revelation model described in
`prd-axi-cli.md §Progressive Disclosure`, refined per the 2026-05-03
design conversation that distinguished **competency** ("how deep into
my surface have I gone") from **role** ("is this surface even FOR me").

Three filtering axes:

1. **Role** — user-declared list in `~/.axi/competency.json`. Each role
   activates a set of activity intents. A `basic` user activates only
   `start` (the universal end-user floor: chat, note, memory, search,
   doctor, config). A `researcher` adds `research` + `investigate`.
   A `builder` adds `build` + `maintain` + `investigate`. Roles stack;
   a user who's both `researcher` and `instructor` sees the union.

2. **Intent group** — manifest-declared on each command (AEOS schema
   field `intent_groups`). A command surfaces only when at least one
   of its intents is activated by one of the user's roles.

3. **Competency tier** — manifest-declared `tier` per command, filtered
   against the user's per-role competency. Cumulative: starter ⊂ core
   ⊂ advanced ⊂ internal. New extensions start at `starter` for that
   user even if they're advanced globally elsewhere.

Reveal flags widen the surface deliberately:
- ``--all`` — every command except `internal`
- ``--tier <starter|core|advanced|internal>`` — ceiling override
- ``--internal`` — opt into `internal` (combine with above)
- ``--group <intent>`` — filter to a single intent group
- ``--role <role>`` — temporarily expand active roles for one command
- Tab-tab completion (existing argcomplete behavior) — discovery is
  unconditional; the filtering only applies to listing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Tier model — competency ladder within a role.
TIERS = ("starter", "core", "advanced", "internal")
DEFAULT_TIER = "starter"
DEFAULT_CMD_TIER = "core"  # Commands with no manifest declaration

# Role model — user-declared in ~/.axi/competency.json.
ROLES = (
    "basic",
    "researcher",
    "instructor",
    "student",
    "operator",
    "builder",
    "admin",
    "steward",
)
DEFAULT_ROLE = "basic"

# Activity intents — manifest-declared on commands. The role → intent
# map is the join: a command surfaces when (cmd.intent_groups ∩
# expand_intents(user.roles)) ≠ ∅. Per the 2026-05-03 design, `start`
# is the universal end-user floor and every role inherits it.
INTENTS = (
    "start",
    "research",
    "teach",
    "learn",
    "operate",
    "build",
    "maintain",
    "govern",
    "investigate",
)
DEFAULT_CMD_INTENTS: tuple[str, ...] = ()  # empty = universal fallback (matches any role)

# Canonical role → activated intents map.  Locked per the 2026-05-03
# design conversation; future role additions append here without
# disturbing existing mappings.
ROLE_INTENT_MAP: dict[str, frozenset[str]] = {
    "basic":      frozenset({"start"}),
    "researcher": frozenset({"start", "research", "investigate"}),
    "instructor": frozenset({"start", "teach", "investigate"}),
    "student":    frozenset({"start", "learn"}),
    "operator":   frozenset({"start", "operate"}),
    "builder":    frozenset({"start", "build", "maintain", "investigate"}),
    "admin":      frozenset({"start", "maintain", "govern"}),
    "steward":    frozenset({"start", "govern", "maintain", "investigate"}),
}

COMPETENCY_FILE = "competency.json"


@dataclass(frozen=True)
class UserCompetency:
    """User's role membership + competency ceilings.

    Loaded from `~/.axi/competency.json`. Missing file → `basic` role +
    `starter` global, no overrides — true mom-and-pop floor.
    """

    roles: tuple[str, ...] = (DEFAULT_ROLE,)
    global_tier: str = DEFAULT_TIER
    per_extension: dict = field(default_factory=dict)

    def expand_intents(self) -> frozenset[str]:
        """Union of intents activated by the user's declared roles."""
        out: set[str] = set()
        for role in self.roles:
            out.update(ROLE_INTENT_MAP.get(role, frozenset()))
        # Safety floor: even an unrecognised role list still gets `start`,
        # so the user never ends up with zero visible commands.
        if not out:
            out.add("start")
        return frozenset(out)

    def effective_tier_for_extension(self, extension: str) -> str:
        """Lower (more conservative) of global vs per-extension override.

        A new extension stays at `starter` even for a globally-advanced
        user, until they earn familiarity with it (PRD invariant).
        """
        per = self.per_extension.get(extension)
        if per is None:
            return self.global_tier
        return _lower_tier(self.global_tier, per)

    def to_dict(self) -> dict:
        return {
            "roles": list(self.roles),
            "global": self.global_tier,
            "per_extension": dict(self.per_extension),
        }


def _tier_index(tier: str) -> int:
    try:
        return TIERS.index(tier)
    except ValueError:
        return TIERS.index("internal")


def _lower_tier(a: str, b: str) -> str:
    return a if _tier_index(a) <= _tier_index(b) else b


def _resolve_state_dir(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return state_dir
    try:
        from axiom.infra.paths import get_user_state_dir
        return get_user_state_dir()
    except Exception:
        return Path.home() / ".axi"


def load_competency(state_dir: Path | None = None) -> UserCompetency:
    """Read `~/.axi/competency.json`. Missing → defaults (basic + starter).

    `state_dir` is for tests; production passes None.
    """
    path = _resolve_state_dir(state_dir) / COMPETENCY_FILE
    if not path.exists():
        return UserCompetency()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("competency.json unreadable, defaulting: %s", exc)
        return UserCompetency()

    raw_roles = data.get("roles") or [DEFAULT_ROLE]
    valid_roles = tuple(r for r in raw_roles if r in ROLES) or (DEFAULT_ROLE,)
    global_tier = data.get("global", DEFAULT_TIER)
    if global_tier not in TIERS:
        global_tier = DEFAULT_TIER
    per: dict[str, str] = {}
    for ext, tier in (data.get("per_extension") or {}).items():
        if tier in TIERS:
            per[ext] = tier
    return UserCompetency(
        roles=valid_roles,
        global_tier=global_tier,
        per_extension=per,
    )


def save_competency(c: UserCompetency, state_dir: Path | None = None) -> Path:
    """Persist competency to `~/.axi/competency.json`. Returns the path."""
    path = _resolve_state_dir(state_dir) / COMPETENCY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c.to_dict(), indent=2) + "\n")
    return path


def cmd_meets_tier(cmd_info: dict, user_tier: str) -> bool:
    """Does this command surface at the user's tier ceiling?"""
    cmd_tier = cmd_info.get("tier") or DEFAULT_CMD_TIER
    return _tier_index(cmd_tier) <= _tier_index(user_tier)


def cmd_meets_intents(
    cmd_info: dict,
    activated_intents: frozenset[str],
) -> bool:
    """Does this command's intent set overlap with the user's activated
    intents?

    Commands declaring no intents are treated as **universal fallback** —
    they surface for every role (any user can call them) but don't
    belong in a specific intent group, so the renderer puts them under
    'Other:' rather than miscategorising them as bootstrap.
    """
    declared = cmd_info.get("intent_groups") or []
    if not declared:
        return True  # universal — show to everyone
    return any(i in activated_intents for i in declared)


def filter_commands(
    commands: dict[str, dict],
    *,
    user_competency: UserCompetency | None = None,
    role_override: tuple[str, ...] | None = None,
    tier_override: str | None = None,
    intent_group: str | None = None,
    show_all: bool = False,
    include_internal: bool = False,
) -> dict[str, dict]:
    """Filter discovered commands by role + intent + tier.

    Args:
        commands: output of `discover_cli_commands`.
        user_competency: from `load_competency()`. Defaults if None.
        role_override: temporarily expand active roles for this call —
            used by `axi --role <r>` to peek at a different role's
            surface without persisting.
        tier_override: caller-supplied tier ceiling (e.g. `--tier
            advanced`). Wins over user's competency.
        intent_group: filter to commands declaring this intent group
            (cross-role rollup, e.g. `--group maintain`).
        show_all: ignore role + tier filtering — surface everything that
            isn't `internal`. `internal` still requires
            `include_internal=True`.
        include_internal: opt into surfacing `internal` commands.

    Returns: filtered subset of `commands`, same shape.
    """
    if user_competency is None:
        user_competency = load_competency()

    if role_override:
        # Build a synthetic competency with the override roles, keeping
        # the user's competency tier intact.
        active_competency = UserCompetency(
            roles=tuple(role_override),
            global_tier=user_competency.global_tier,
            per_extension=dict(user_competency.per_extension),
        )
    else:
        active_competency = user_competency

    activated = active_competency.expand_intents()
    out: dict[str, dict] = {}
    for noun, info in commands.items():
        cmd_tier = info.get("tier") or DEFAULT_CMD_TIER
        # internal gating — even with --all, internal stays hidden unless
        # explicitly opted into.
        if cmd_tier == "internal" and not include_internal:
            continue

        if not show_all:
            # Role + intent join.
            if intent_group is not None:
                # --group filter: short-circuits role; user is asking for
                # everything in that intent regardless of role membership.
                if intent_group not in (info.get("intent_groups") or DEFAULT_CMD_INTENTS):
                    continue
            else:
                if not cmd_meets_intents(info, activated):
                    continue

            # Tier ceiling.
            if tier_override is not None:
                ceiling = tier_override
            else:
                ceiling = active_competency.effective_tier_for_extension(
                    info.get("extension", ""),
                )
            if not cmd_meets_tier(info, ceiling):
                continue

        out[noun] = info
    return out


def group_by_intent(commands: dict[str, dict]) -> dict[str, list[str]]:
    """Reverse-index: intent → sorted list of nouns declaring it.

    Useful for `axi help` rendering — group commands by activity rather
    than dumping a flat list.
    """
    groups: dict[str, list[str]] = {}
    for noun, info in commands.items():
        for g in (info.get("intent_groups") or DEFAULT_CMD_INTENTS):
            groups.setdefault(g, []).append(noun)
    for g in groups:
        groups[g].sort()
    return groups


def is_quiet() -> bool:
    """`AXI_HELP_FLAT=1` → bypass filtering for CI scripts."""
    return bool(os.environ.get("AXI_HELP_FLAT"))


__all__ = [
    "DEFAULT_CMD_INTENTS",
    "DEFAULT_CMD_TIER",
    "DEFAULT_ROLE",
    "DEFAULT_TIER",
    "INTENTS",
    "ROLES",
    "ROLE_INTENT_MAP",
    "TIERS",
    "UserCompetency",
    "cmd_meets_intents",
    "cmd_meets_tier",
    "filter_commands",
    "group_by_intent",
    "is_quiet",
    "load_competency",
    "save_competency",
]
