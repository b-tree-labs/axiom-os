# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dynamic rollup of every installed extension's CLI verbs + slash commands.

Two sources of truth merged into a single `CommandTree`:

1. **CLI verbs** — `axi <noun> <verb>` form. Pulled from each extension's
   AEOS `[[extension.provides]] kind = "cmd"` blocks via
   `axiom.extensions.discovery.discover_cli_commands()`. For each noun, the
   entry module's `build_parser()` is imported and walked to extract verbs.

2. **Chat slash commands** — `/<command>` form available inside `axi chat`.
   Pulled from the chat extension's `get_slash_commands()` registry, which
   itself auto-syncs CLI commands and adds chat-meta commands.

Conflict resolution mirrors Axiom's existing 3-tier scope hierarchy:
**builtin < user < project**. Higher tier wins; loser is recorded on the
`Conflict` so callers can surface it. Within the same tier, alphabetical-
by-extension-name wins (deterministic).

Escape hatch: `<ext>:<command>` namespace form is always available — even
when the bare form collides, `/hygiene:status` resolves unambiguously.
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass, field
from typing import Any

# Tier ordering — higher index = higher precedence.
_TIER_ORDER = ("builtin", "user", "project")


@dataclass(frozen=True)
class Verb:
    """A single CLI verb under some noun (e.g. `axi hygiene list worktrees`)."""

    name: str
    help: str = ""
    args: tuple[str, ...] = ()  # positional arg names; flags omitted


@dataclass(frozen=True)
class CliNoun:
    """A noun (e.g. `tidy`) with its verbs, sourced from one extension."""

    noun: str
    extension: str
    description: str
    module: str  # import path, e.g. `axiom.extensions.builtins.hygiene.cli`
    function: str  # entry function name, default `main`
    tier: str  # "builtin" | "user" | "project"
    verbs: tuple[Verb, ...] = ()


@dataclass(frozen=True)
class SlashCommand:
    """A chat slash command (e.g. `/help`, `/permissions`)."""

    name: str  # without leading slash
    extension: str
    description: str = ""
    tier: str = "builtin"


@dataclass
class Conflict:
    """One losing definition shadowed by a winner."""

    key: str  # the colliding noun/slash name
    winner_extension: str
    loser_extension: str
    reason: str  # "lower-tier" | "alphabetical-tiebreak"


@dataclass
class CommandTree:
    """Resolved cross-harness command surface."""

    nouns: dict[str, CliNoun] = field(default_factory=dict)
    slash_commands: dict[str, SlashCommand] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    def namespaced_noun(self, ext: str, noun: str) -> str:
        """Return the disambiguating form `<ext>:<noun>`."""
        return f"{ext}:{noun}"


# ---------------------------------------------------------------------------
# Verb extraction from build_parser()
# ---------------------------------------------------------------------------


def _extract_verbs(module_path: str, function: str = "main") -> tuple[Verb, ...]:
    """Import the entry module and extract subparser verbs from build_parser().

    Convention: each Axiom CLI module exposes `build_parser() -> ArgumentParser`
    that adds a `subparsers` action. We walk that action to find verb names +
    help text. If `build_parser` is absent or import fails, returns ().
    """
    try:
        mod = importlib.import_module(module_path)
    except Exception:
        return ()
    builder = getattr(mod, "build_parser", None)
    if builder is None or not callable(builder):
        return ()
    try:
        parser: argparse.ArgumentParser = builder()
    except Exception:
        return ()

    verbs: list[Verb] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for verb_name, sub in action.choices.items():
            help_text = ""
            for choice_action in action._choices_actions:
                if choice_action.dest == verb_name:
                    help_text = choice_action.help or ""
                    break
            args = tuple(
                a.dest
                for a in sub._actions
                if not a.option_strings  # positional only
                and a.dest not in ("help",)
            )
            verbs.append(Verb(name=verb_name, help=help_text, args=args))
    return tuple(sorted(verbs, key=lambda v: v.name))


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


def _tier_for(ext_root: str, builtin: bool) -> str:
    """Classify an extension by its on-disk location."""
    if builtin:
        return "builtin"
    if "/.axi/extensions/" in ext_root or "\\.axi\\extensions\\" in ext_root:
        return "user"
    return "project"


def _tier_rank(tier: str) -> int:
    try:
        return _TIER_ORDER.index(tier)
    except ValueError:
        return -1


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------


def _resolve_conflict(
    existing: CliNoun | SlashCommand,
    candidate: CliNoun | SlashCommand,
) -> tuple[CliNoun | SlashCommand, Conflict]:
    """Pick the winner between two competing entries with the same name.

    Higher tier wins; alphabetical-by-extension-name tiebreaks within tier.
    Returns (winner, conflict_record).
    """
    e_rank = _tier_rank(existing.tier)
    c_rank = _tier_rank(candidate.tier)
    if c_rank > e_rank:
        winner, loser, reason = candidate, existing, "lower-tier"
    elif c_rank < e_rank:
        winner, loser, reason = existing, candidate, "lower-tier"
    else:
        # Same tier — alphabetical by extension name
        if candidate.extension < existing.extension:
            winner, loser, reason = candidate, existing, "alphabetical-tiebreak"
        else:
            winner, loser, reason = existing, candidate, "alphabetical-tiebreak"

    key = getattr(winner, "noun", None) or getattr(winner, "name", "?")
    return winner, Conflict(
        key=key,
        winner_extension=winner.extension,
        loser_extension=loser.extension,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------


def discover_command_tree(
    cli_commands: dict[str, dict[str, Any]]
    | list[tuple[str, dict[str, Any]]]
    | None = None,
    slash_commands: dict[str, str] | None = None,
) -> CommandTree:
    """Build the CommandTree by rolling up CLI verbs + slash commands.

    `cli_commands` accepts either:
      - a `dict[noun -> meta]` (e.g. from `discover_cli_commands()` which has
        already been deduplicated upstream) — no conflict surface, but useful
        for the common case
      - a `list[(noun, meta)]` of every claim made by every extension, which
        preserves duplicates so this layer can resolve conflicts honestly

    Both sources are injectable so tests can exercise the conflict path.
    """
    tree = CommandTree()

    if cli_commands is None:
        pairs = _live_cli_command_pairs()
    elif isinstance(cli_commands, dict):
        pairs = list(cli_commands.items())
    else:
        pairs = list(cli_commands)

    slash_commands = (
        slash_commands if slash_commands is not None else _live_slash_commands()
    )

    # CLI nouns → verbs
    for noun, meta in sorted(pairs, key=lambda p: (p[0], p[1].get("extension", ""))):
        tier = _tier_for(meta.get("root", ""), bool(meta.get("builtin", False)))
        candidate = CliNoun(
            noun=noun,
            extension=str(meta.get("extension", "?")),
            description=str(meta.get("description", "")),
            module=str(meta.get("module", "")),
            function=str(meta.get("function", "main")),
            tier=tier,
            verbs=_extract_verbs(
                str(meta.get("module", "")), str(meta.get("function", "main"))
            ),
        )
        existing = tree.nouns.get(noun)
        if existing is None:
            tree.nouns[noun] = candidate
            continue
        winner, conflict = _resolve_conflict(existing, candidate)
        tree.nouns[noun] = winner  # type: ignore[assignment]
        tree.conflicts.append(conflict)

    # Chat slash commands
    for raw_name, description in sorted(slash_commands.items()):
        name = raw_name.lstrip("/").split()[0]  # `/save title` → `save`
        candidate_sc = SlashCommand(
            name=name,
            extension="chat",  # chat ext owns the registry; per-ext slash kinds future work
            description=description,
            tier="builtin",
        )
        existing_sc = tree.slash_commands.get(name)
        if existing_sc is None:
            tree.slash_commands[name] = candidate_sc
            continue
        winner_sc, conflict_sc = _resolve_conflict(existing_sc, candidate_sc)
        tree.slash_commands[name] = winner_sc  # type: ignore[assignment]
        tree.conflicts.append(conflict_sc)

    return tree


def _live_cli_commands() -> dict[str, dict[str, Any]]:
    """Default source: walk installed extensions for `kind=cmd` blocks (deduped)."""
    from axiom.extensions.discovery import discover_cli_commands

    return discover_cli_commands()


def _live_cli_command_pairs() -> list[tuple[str, dict[str, Any]]]:
    """Walk installed extensions and emit every (noun, meta) pair, preserving
    duplicates so this layer can detect and resolve cross-extension conflicts.
    """
    from axiom.extensions.discovery import discover_extensions

    pairs: list[tuple[str, dict[str, Any]]] = []
    for ext in discover_extensions():
        if not ext.enabled:
            continue
        for cmd in ext.cli_commands:
            pairs.append(
                (
                    cmd.noun,
                    {
                        "module": cmd.module,
                        "function": cmd.function or "main",
                        "description": cmd.description,
                        "extension": ext.name,
                        "root": str(ext.root),
                        "builtin": ext.builtin,
                    },
                )
            )
    return pairs


def _live_slash_commands() -> dict[str, str]:
    """Default source: chat extension's get_slash_commands() registry."""
    try:
        from axiom.extensions.builtins.chat.commands import get_slash_commands

        return get_slash_commands()
    except Exception:
        return {}
