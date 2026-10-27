# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One completion engine for slash commands, shared by the full-screen TUI and the line REPL.

``suggest(line, agent, surface)`` is a pure function over what the registry (and the CLI namespaces and
user commands behind ``get_slash_commands``) says this viewer may run. The prompt_toolkit pieces below are
thin adapters over it: a ``Completer`` for the dropdown and an ``AutoSuggest`` for the gray ghost text that
Tab / Right-arrow / End accept. Nothing here knows a command by name; a command offers its arguments
through ``Command.complete`` (see ``completers.py``).

Ghost text and the dropdown only ever act on a line that begins with ``/``. Prose is never completed, so
nothing the person is writing can be echoed back as a suggestion.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Usage counters are tiny and bounded: the N most-used entries survive.
MAX_USAGE_ENTRIES = 100
_USAGE_FILE = "chat_completion_usage.json"

# Ranking tiers, lower is better.
_T_EXACT, _T_PREFIX, _T_SUBSTRING, _T_FUZZY = 0, 1, 2, 3


@dataclass(frozen=True)
class Suggestion:
    value: str  # the completed word: "/model", "rename", "groq"
    description: str = ""
    kind: str = "command"  # command | subcommand | argument
    replace: int = 0  # characters of the line, back from the cursor, that ``value`` replaces

    def remainder(self, line: str) -> str:
        """The characters ghost text adds after ``line``; empty unless ``value`` extends what was typed."""
        partial = line[len(line) - self.replace :] if self.replace else ""
        if self.value.lower().startswith(partial.lower()) and len(self.value) > len(partial):
            return self.value[len(partial) :]
        return ""


# ---- usage counter ---------------------------------------------------------------------------------

_usage_cache: tuple[str, int, dict[str, int]] | None = None


def _usage_path() -> str:
    from axiom.infra.paths import get_user_state_dir

    return str(get_user_state_dir() / _USAGE_FILE)


def load_usage() -> dict[str, int]:
    """Command usage counts. Missing or corrupt state is an empty counter, never an error."""
    global _usage_cache
    try:
        path = _usage_path()
        mtime = os.stat(path).st_mtime_ns
        if _usage_cache and _usage_cache[0] == path and _usage_cache[1] == mtime:
            return _usage_cache[2]
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        data = {str(k): int(v) for k, v in raw.items() if isinstance(v, int | float)}
        _usage_cache = (path, mtime, data)
        return data
    except (OSError, ValueError, AttributeError):
        return {}


def record_use(line: str) -> None:
    """Count a submitted slash line (its command, and command + subcommand). Never records arguments."""
    global _usage_cache
    parts = line.split()
    if not parts or not parts[0].startswith("/"):
        return
    keys = [parts[0].lower()]
    if len(parts) > 1 and re.fullmatch(r"[a-z][\w-]*", parts[1].lower()):
        keys.append(f"{keys[0]} {parts[1].lower()}")
    try:
        usage = dict(load_usage())
        for k in keys:
            usage[k] = usage.get(k, 0) + 1
        if len(usage) > MAX_USAGE_ENTRIES:
            keep = sorted(usage.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_USAGE_ENTRIES]
            usage = dict(keep)
        path = _usage_path()
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(usage, fh)
        os.replace(tmp, path)
        _usage_cache = None
    except OSError as exc:  # a read-only home must not break the chat
        log.debug("completion usage not recorded: %s", exc)


# ---- matching --------------------------------------------------------------------------------------


def _subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


def _tier(partial: str, candidate: str) -> int | None:
    p, c = partial.lower().lstrip("/"), candidate.lower().lstrip("/")
    if not p:
        return _T_PREFIX
    if c == p:
        return _T_EXACT
    if c.startswith(p):
        return _T_PREFIX
    if len(p) >= 2 and p in c:
        return _T_SUBSTRING
    if len(p) >= 2 and _subsequence(p, c):
        return _T_FUZZY
    return None


def _rank(items: list[tuple[str, str, str, str]], partial: str, usage: dict[str, int]):
    """items = (value, description, kind, usage_key). Returns ranked Suggestions."""
    scored = []
    for order, (value, desc, kind, ukey) in enumerate(items):
        tier = _tier(partial, value)
        if tier is None:
            continue
        scored.append(
            (tier, -usage.get(ukey, 0), order, Suggestion(value, desc, kind, len(partial)))
        )
    scored.sort(key=lambda t: t[:3])
    return [s for *_, s in scored]


# ---- the engine ------------------------------------------------------------------------------------


def _visible(agent: Any, surface: str):
    """The commands a viewer may see, and the registry's view of who may run what."""
    from .builtin_commands import ensure_loaded
    from .command_registry import REGISTRY
    from .commands import _viewer_is_admin, get_slash_commands

    ensure_loaded()
    meta = get_slash_commands(agent, surface)
    admin = _viewer_is_admin(agent, surface)
    return REGISTRY, meta, REGISTRY.commands(admin=admin)


def _runs_here(registry: Any, name: str, surface: str) -> bool:
    cmd = registry.get(name)
    return cmd is None or surface in cmd.surfaces


def suggest(line: str, agent: Any = None, surface: str = "repl") -> list[Suggestion]:
    """Ranked completions for the word being typed at the end of ``line``. Empty for anything but a slash line."""
    if not line.startswith("/") or "\n" in line:
        return []
    try:
        registry, meta, visible = _visible(agent, surface)
        usage = load_usage()
        ahead = _follow_up_suggestions(line, agent, registry, visible, surface)
        if " " not in line:
            return ahead + _command_names(line, registry, meta, visible, surface, usage)
        if ahead:
            return ahead + _arguments(line, agent, registry, meta, visible, surface, usage)
        return _arguments(line, agent, registry, meta, visible, surface, usage)
    except Exception as exc:  # noqa: BLE001
        log.debug("suggest(%r) failed: %s", line, exc)
        return []


def _follow_up_suggestions(line: str, agent: Any, registry: Any, visible: Any, surface: str) -> list[Suggestion]:
    """After a result, the command it offered as the next step leads the list.

    Only a follow-up that is complete (nothing left to fill in), that this viewer may run here, and that
    extends what has been typed is offered, so ghost text never proposes a line the person cannot send.
    """
    out: list[Suggestion] = []
    names = {n.lower() for c in visible for n in (c.name, *c.aliases)}
    for fu in getattr(agent, "_last_follow_ups", None) or ():
        cmd = fu.command
        if not fu.concrete or not cmd.startswith("/") or len(cmd) <= len(line):
            continue
        if not cmd.lower().startswith(line.lower()):
            continue
        if cmd.split()[0].lower() not in names or not _runs_here(registry, cmd.split()[0], surface):
            continue
        out.append(Suggestion(cmd, fu.why, "follow-up", len(line)))
    return out


def _arguments(line, agent, registry, meta, visible, surface, usage):
    try:
        head, _, rest = line.partition(" ")
        head_l = head.lower()
        cmd = registry.get(head_l)
        known = {k.split()[0].lower() for k in meta}
        visible_names = {n.lower() for c in visible for n in (c.name, *c.aliases)}
        if head_l not in known and head_l not in visible_names:
            return []  # unknown, hidden, or admin-only for this viewer: nothing to offer
        if not _runs_here(registry, head_l, surface):
            return []
        words = rest.split(" ")
        partial, finished = words[-1], [w for w in words[:-1] if w]
        items: list[tuple[str, str, str, str]] = []
        seen: set[str] = set()
        if not finished:
            canon = cmd.name.lower() if cmd else head_l
            for key, text in meta.items():
                if key.lower().startswith(canon + " "):
                    sub = key.split(" ", 1)[1]
                    if " " not in sub and sub not in seen:
                        seen.add(sub)
                        items.append((sub, text, "subcommand", f"{canon} {sub.lower()}"))
        if cmd is not None and cmd.complete is not None:
            try:
                offered = cmd.complete(agent, finished) or []
            except Exception as exc:  # noqa: BLE001 - a broken completer must not break typing
                log.debug("completer for %s failed: %s", head_l, exc)
                offered = []
            for value, desc in offered:
                if value not in seen:
                    seen.add(value)
                    items.append((value, desc, "argument", ""))
        return _rank(items, partial, usage)
    except Exception as exc:  # noqa: BLE001
        log.debug("arguments for %r failed: %s", line, exc)
        return []


def _command_names(line, registry, meta, visible, surface, usage):
    items: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for key, text in meta.items():
        if " " in key or key in seen or not _runs_here(registry, key, surface):
            continue
        seen.add(key)
        items.append((key, text, "command", key.lower()))
    for c in visible:
        if surface not in c.surfaces:
            continue
        for alias in c.aliases:
            if alias not in seen:
                seen.add(alias)
                items.append((alias, f"{c.summary} (alias of {c.name})", "command", alias.lower()))
    return _rank(items, line, usage)


def best_remainder(line: str, agent: Any = None, surface: str = "repl") -> str:
    """The gray text for ``line``: what the best prefix-matching suggestion adds. '' when there is none."""
    for s in suggest(line, agent, surface):
        rem = s.remainder(line)
        if rem:
            return rem
        # Only the leading candidates are worth ghosting; a fuzzy match is not an extension of the text.
        break
    return ""


# ---- prompt_toolkit adapters -----------------------------------------------------------------------

try:  # pragma: no cover - exercised by tests when prompt_toolkit is installed
    from prompt_toolkit.auto_suggest import AutoSuggest
    from prompt_toolkit.auto_suggest import Suggestion as _PtkSuggestion
    from prompt_toolkit.completion import Completer, Completion

    _PTK = True
except ImportError:  # pragma: no cover
    AutoSuggest = object  # type: ignore[assignment,misc]
    Completer = object  # type: ignore[assignment,misc]
    _PTK = False


def _resolve(agent_or_fn: Any) -> Any:
    return (
        agent_or_fn()
        if callable(agent_or_fn) and not hasattr(agent_or_fn, "session")
        else agent_or_fn
    )


class SlashAutoSuggest(AutoSuggest):  # type: ignore[misc,valid-type]
    """Gray ghost text: the rest of the best completion, for slash lines only."""

    def __init__(self, agent: Any = None, surface: str = "repl") -> None:
        self._agent = agent
        self._surface = surface

    def get_suggestion(self, buffer: Any, document: Any):
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in document.text or not document.is_cursor_at_the_end:
            return None
        rem = best_remainder(text, _resolve(self._agent), self._surface)
        return _PtkSuggestion(rem) if rem else None


class SlashCompleter(Completer):  # type: ignore[misc,valid-type]
    """The dropdown: the same ranked suggestions, with descriptions as meta."""

    def __init__(self, agent: Any = None, surface: str = "repl") -> None:
        self._agent = agent
        self._surface = surface

    def get_completions(self, document: Any, complete_event: Any):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for s in suggest(text, _resolve(self._agent), self._surface):
            yield Completion(
                s.value,
                start_position=-s.replace,
                display=s.value,
                display_meta=s.description,
            )


def make_autosuggest(agent: Any = None, surface: str = "repl"):
    """A ghost-text provider, or None when prompt_toolkit is not installed."""
    return SlashAutoSuggest(agent, surface) if _PTK else None


def make_completer(agent: Any = None, surface: str = "repl"):
    return SlashCompleter(agent, surface) if _PTK else None


def accept_suggestion(buffer: Any) -> bool:
    """Insert the buffer's ghost text (Tab / Right / End). True when something was accepted."""
    sug = getattr(buffer, "suggestion", None)
    if sug is not None and sug.text and buffer.document.is_cursor_at_the_end:
        buffer.insert_text(sug.text)
        return True
    return False
