# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every command result leads to its follow-up (ADR-168).

A result that stops at a fact leaves the person to guess what to type next. This module is the one shared
layer that closes that gap:

* ``FollowUp(command, why)`` is a real command line and the reason to run it.
* A command **declares** the follow-ups it can offer, in its own module, as ``Command.follow_ups``
  (a ``{key: FollowUp}`` template map; ``{name}`` placeholders are filled at call time).
* A handler **emits** them with ``result(command, text, "key", name=...)``. A key that was not declared is
  an error, so a handler cannot invent a follow-up the declaration (and the registry test) never saw.
* ``CommandResult`` is a ``str`` subclass: it prints exactly like the text it wraps, so every surface that
  handled strings keeps working, while a surface that looks can read ``.follow_ups`` and ``.data``.
  The rendered "Next:" footer is part of the text.
* The dispatcher remembers the latest follow-ups on the agent (``agent._last_follow_ups``) so the
  autocomplete engine can suggest the likeliest next command.
* ``remember_list`` / ``resolve_ref`` keep the last numbered list per session so ``/tools 3`` resolves.
* ``validate_registry`` walks the registry and reports every follow-up that names a command, subcommand or
  flag that does not exist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

_PLACEHOLDER = re.compile(r"\{[a-z_]+\}|<[^>]+>")


@dataclass(frozen=True)
class FollowUp:
    command: str  # real syntax: "/rag on", or "<cli> settings set chat.tool_namespaces press"
    why: str

    def to_dict(self) -> dict[str, str]:
        return {"command": self.command, "why": self.why}

    def filled(self, **values: Any) -> FollowUp:
        try:
            return FollowUp(self.command.format(**values), self.why.format(**values))
        except KeyError as exc:  # a template used a placeholder the caller did not supply
            raise KeyError(f"follow-up {self.command!r} needs a value for {exc}") from exc

    @property
    def concrete(self) -> bool:
        """True when nothing is left for the person to fill in (a ghost-text candidate)."""
        return not _PLACEHOLDER.search(self.command)


class CommandResult(str):
    """The text a command prints, plus the structured follow-ups and data behind it."""

    follow_ups: tuple[FollowUp, ...]
    data: Any

    def __new__(cls, text: str, follow_ups: Iterable[FollowUp] = (), data: Any = None):
        follow_ups = tuple(follow_ups)
        obj = super().__new__(cls, text + render_footer(follow_ups) if follow_ups else text)
        obj.follow_ups = follow_ups
        obj.data = data
        obj.body = text
        return obj

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.body,
            "data": self.data,
            "follow_ups": [f.to_dict() for f in self.follow_ups],
        }


def render_footer(follow_ups: Sequence[FollowUp]) -> str:
    """``Next:`` and one line per follow-up. Short on purpose: three lines at most are shown."""
    if not follow_ups:
        return ""
    lines = ["  Next:"]
    for f in follow_ups[:3]:
        lines.append(f"    {f.command}  ({f.why})")
    return "\n" + "\n".join(lines) + "\n"


def follow_ups_of(result: Any) -> tuple[FollowUp, ...]:
    return tuple(getattr(result, "follow_ups", ()) or ())


def with_json_follow_ups(payload: dict[str, Any], follow_ups: Sequence[FollowUp]) -> dict[str, Any]:
    """``--json`` output carries the same follow-ups the text footer shows."""
    out = dict(payload)
    out["follow_ups"] = [f.to_dict() for f in follow_ups]
    return out


def result(
    command_name: str,
    text: str,
    *keys: str | tuple[str, dict[str, Any]],
    data: Any = None,
    registry: Any = None,
    **values: Any,
) -> CommandResult:
    """Build a result from the command's own declared follow-ups.

    ``keys`` name entries in ``Command.follow_ups``; a ``(key, values)`` pair fills that entry with its own
    values, and ``values`` fills every entry. Raises ``KeyError`` for an undeclared key.
    """
    follow_ups = pick(command_name, *keys, registry=registry, **values)
    return CommandResult(text, follow_ups, data)


def _cli_label() -> str:
    return _cli_name()


def pick(
    command_name: str, *keys: str | tuple[str, dict[str, Any]], registry: Any = None, **values: Any
) -> tuple[FollowUp, ...]:
    if registry is None:
        from .builtin_commands import ensure_loaded
        from .command_registry import REGISTRY

        ensure_loaded()
        registry = REGISTRY
    cmd = registry.get(command_name)
    if cmd is None:
        raise KeyError(f"{command_name} is not a registered command")
    out: list[FollowUp] = []
    values = {"cli": _cli_name(), **values}
    for k in keys:
        key, own = (k, {}) if isinstance(k, str) else k
        if key not in cmd.follow_ups:
            raise KeyError(f"{command_name} did not declare a follow-up {key!r}")
        out.append(cmd.follow_ups[key].filled(**{**values, **own}))
    return tuple(out)


# ---- the last numbered list in a session ---------------------------------------------------------------


def remember_list(agent: Any, kind: str, keys: Sequence[str]) -> None:
    """Keep the stable identifiers of the rows just shown, in order, so ``/<kind> 3`` means row 3."""
    try:
        lists = getattr(agent, "_last_lists", None)
        if not isinstance(lists, dict):
            lists = {}
            agent._last_lists = lists
        lists[kind] = list(keys)
    except Exception:  # noqa: BLE001 - remembering a list never fails a command
        pass


def resolve_ref(agent: Any, kind: str, token: str, fallback: Any = None) -> tuple[str | None, str]:
    """Resolve a row number to its identifier. ``(key, "")`` or ``(None, sentence)``.

    A token that is not a number is not this function's business: it returns ``(token, "")`` unchanged.
    The remembered list wins; ``fallback`` (a callable returning the keys in their stable order) covers a
    session that has not listed yet, for commands whose numbering is deterministic.
    """
    if not token.isdigit():
        return token, ""
    keys = (getattr(agent, "_last_lists", None) or {}).get(kind)
    if not keys and fallback is not None:
        keys = list(fallback())
    if not keys:
        return None, f"There is no list to take number {token} from yet. Run the list command first."
    n = int(token)
    if not 1 <= n <= len(keys):
        return None, f"There is no row {n}; the last list had {len(keys)}."
    return keys[n - 1], ""


# ---- registry validation -------------------------------------------------------------------------------


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


#: The CLI nouns a follow-up may name. A closed list, checked against the real CLI registry by a test:
#: asking the registry here would import every CLI module, which is slow and has hung on some interpreters.
KNOWN_CLI_NOUNS: frozenset[str] = frozenset({"settings", "secrets", "chat", "mcp"})


def _known_cli_nouns() -> set[str]:
    return set(KNOWN_CLI_NOUNS)


def check_follow_up(registry: Any, owner: str, key: str, fu: FollowUp) -> str | None:
    """None when the follow-up names something real; else why it does not."""
    line = _PLACEHOLDER.sub("ARG", fu.command).split()
    if not line:
        return f"{owner}[{key}]: empty command"
    head = line[0]
    if fu.command.startswith("{cli} ") or head in (_cli_name(), "axi"):
        nouns = _known_cli_nouns()
        if len(line) < 2:
            return f"{owner}[{key}]: a CLI follow-up must name a command"
        if nouns and line[1] not in nouns and line[1] != "ARG":
            return f"{owner}[{key}]: {line[1]!r} is not a CLI command"
        return None
    if not head.startswith("/"):
        return f"{owner}[{key}]: {fu.command!r} is neither a slash command nor a CLI command"
    cmd = registry.get(head)
    if cmd is None:
        return f"{owner}[{key}]: {head} is not a registered command"
    for word in line[1:]:
        if word == "ARG" or word.isdigit():
            return None
        if word.startswith("-"):
            flag = word.split("=")[0]
            known = (
                flag in cmd.usage
                or flag in cmd.subcommands
                or (cmd.cli is not None and flag in cmd.cli.flags)
            )
            if not known:
                return f"{owner}[{key}]: {head} has no flag {flag}"
            continue
        if cmd.subcommands and word not in cmd.subcommands:
            return f"{owner}[{key}]: {head} has no subcommand {word!r}"
        return None  # past the subcommand, the rest is an argument
    return None


def validate_registry(registry: Any) -> list[str]:
    """Every problem in every command's declared follow-ups (empty = all name something that exists)."""
    problems: list[str] = []
    for cmd in registry.commands(include_hidden=True):
        for key, fu in cmd.follow_ups.items():
            msg = check_follow_up(registry, cmd.name, key, fu)
            if msg:
                problems.append(msg)
    return problems
