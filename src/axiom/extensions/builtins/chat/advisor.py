# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Post-command advisor hook: OFFERS one tip after a turn, never executes.

After a REPL or TUI turn completes, ``maybe_advise`` asks a short chain of
*contributors* whether they have a one-line next-step suggestion for the
operator, renders at most one of them in a dim style, and returns. That is
the entire authority of this module:

  - A contributor may only OFFER text. Nothing here runs a command, calls
    a tool, changes a setting, or touches the session. ``Advice`` carries
    plain strings only; a callable in any field is rejected at
    construction, so a contributor cannot smuggle behaviour through it.
  - The operator decides what to do with the tip, by typing it themselves.

Safety contract (the same one ``federation_nudge`` established):

  - TTY-only: silent in CI, pipes and non-interactive contexts.
  - Hard budget: contributors run on a worker thread and are joined with a
    timeout carved from one shared budget; a slow contributor is abandoned
    (never awaited) and its late result is dropped.
  - Every exception is swallowed. Chat MUST continue if advice fails.
  - A persisted decline memo (``<state>/chat/advisor_declined.json``) keeps
    a tip the operator muted from coming back; an in-process cooldown keeps
    the same tip from repeating for ``COOLDOWN_TURNS`` turns.
  - At most one tip per turn, whatever the contributors return.
  - ``chat.advisor.enabled`` (``/advisor on|off``) turns the whole hook off;
    when off, ``maybe_advise`` returns before consulting anyone.

Contributors come from two places, in this order: the in-process registry
(``register_advisor``; the builtins use it, so do tests) and then the
``axiom.chat.advisor`` entry-point group for installed extensions. A
contributor is a callable ``(AdviceContext) -> Advice | None``.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ADVISOR_GROUP = "axiom.chat.advisor"

MAX_ADVICE_CHARS = 160
MAX_COMMAND_CHARS = 200
DEFAULT_BUDGET_MS = 400
COOLDOWN_TURNS = 10

OUTCOMES = ("ok", "error", "empty")

ENABLED_SETTING = "chat.advisor.enabled"


# ---------------------------------------------------------------------------
# Data: what a contributor sees, and what it may hand back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdviceContext:
    """What just happened, as much as a contributor is allowed to know.

    ``command`` is the slash command or the operator's turn text, truncated
    by the caller (see ``MAX_COMMAND_CHARS``). ``outcome`` is one of
    ``OUTCOMES``. ``tool_names`` lists the tools the turn actually ran.
    """

    command: str
    outcome: str
    elapsed_ms: int
    session_id: str
    turn_index: int
    tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(
                f"AdviceContext.outcome must be one of {OUTCOMES}, got {self.outcome!r}"
            )


@dataclass(frozen=True)
class Advice:
    """One line of text a contributor OFFERS. Data, never code.

    Every field is a plain string; anything callable is refused so advice
    can never carry behaviour into the render path. ``text`` is collapsed
    to a single line and capped at ``MAX_ADVICE_CHARS``. ``key`` is the
    stable id the decline memo and the cooldown are keyed on.
    """

    text: str
    source: str
    key: str

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if callable(value) or not isinstance(value, str):
                raise TypeError(
                    f"Advice.{f.name} must be a plain string; advice is offered, "
                    "never executed, so it cannot carry a callable"
                )
        text = " ".join(self.text.split())
        if not text:
            raise ValueError("Advice.text is empty")
        if not self.key.strip():
            raise ValueError("Advice.key is empty")
        object.__setattr__(self, "text", text[:MAX_ADVICE_CHARS])


Contributor = Callable[[AdviceContext], "Advice | None"]


# ---------------------------------------------------------------------------
# Contributors: in-process registry first, then the entry-point group
# ---------------------------------------------------------------------------

_REGISTRY: list[tuple[str, Contributor]] = []
_BUILTINS_REGISTERED = False
_EP_CACHE: list[tuple[str, Contributor]] | None = None


def register_advisor(name: str, fn: Contributor) -> None:
    """Register an in-process contributor. Re-registering a name replaces it
    in place so order is stable."""
    if not callable(fn):
        raise TypeError("an advisor contributor must be callable")
    for i, (existing, _) in enumerate(_REGISTRY):
        if existing == name:
            _REGISTRY[i] = (name, fn)
            return
    _REGISTRY.append((name, fn))


def unregister_advisor(name: str) -> None:
    _REGISTRY[:] = [(n, f) for n, f in _REGISTRY if n != name]


def _register_builtins() -> None:
    """The two shipped contributors, in order. Imported lazily so the
    modules that need ``Advice`` can import this one without a cycle."""
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    _BUILTINS_REGISTERED = True
    try:
        from .federation_nudge import federation_advice

        register_advisor("federation", federation_advice)
    except Exception as exc:  # pragma: no cover - import edge cases
        log.warning("federation advisor unavailable: %s", exc)
    try:
        from .slm_advisor import local_slm_advice

        register_advisor("local_slm", local_slm_advice)
    except Exception as exc:  # pragma: no cover - import edge cases
        log.warning("local_slm advisor unavailable: %s", exc)


def _entry_points(group: str) -> Iterable[Any]:
    """Seam over ``importlib.metadata.entry_points`` so tests can stub it."""
    from importlib.metadata import entry_points

    return entry_points(group=group)


def _entry_point_contributors() -> list[tuple[str, Contributor]]:
    """Load the ``axiom.chat.advisor`` group once per process. A missing or
    broken plugin is logged and skipped; it never blocks the others."""
    global _EP_CACHE
    if _EP_CACHE is not None:
        return _EP_CACHE
    found: list[tuple[str, Contributor]] = []
    try:
        eps = list(_entry_points(ADVISOR_GROUP))
    except Exception as exc:  # pragma: no cover - importlib edge cases
        log.warning("advisor entry-points lookup failed: %s", exc)
        eps = []
    for ep in eps:
        try:
            fn = ep.load()
            if not callable(fn):
                raise TypeError("entry point did not resolve to a callable")
            name = str(getattr(ep, "name", "") or getattr(fn, "__name__", "plugin"))
            found.append((name, fn))
        except Exception as exc:
            log.warning("advisor contributor %r failed to load: %s", getattr(ep, "name", ep), exc)
    _EP_CACHE = found
    return found


def _contributors() -> list[tuple[str, Contributor]]:
    """Registry (builtins first) then entry points, de-duplicated by name."""
    _register_builtins()
    seen: set[str] = set()
    out: list[tuple[str, Contributor]] = []
    for name, fn in [*_REGISTRY, *_entry_point_contributors()]:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, fn))
    return out


# ---------------------------------------------------------------------------
# Gates: TTY, the enabled setting
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty()) and bool(sys.stdin.isatty())
    except Exception:
        return False


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def is_enabled() -> bool:
    """``chat.advisor.enabled``; a settings hiccup reads as enabled."""
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return _truthy(SettingsStore().get(ENABLED_SETTING, True))
    except Exception:
        return True


def set_enabled(flag: bool) -> None:
    """Persist ``chat.advisor.enabled`` as a per-user (global-scope) preference."""
    from axiom.extensions.builtins.settings.store import SettingsStore

    SettingsStore().set(ENABLED_SETTING, bool(flag), scope="global")


# ---------------------------------------------------------------------------
# Decline memo (persisted) and cooldown (in-process)
# ---------------------------------------------------------------------------


def _decline_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "chat" / "advisor_declined.json"


def _read_memo() -> dict[str, str]:
    p = _decline_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def has_declined(key: str) -> bool:
    """True if the operator muted this tip before. Same shape as the
    federation decline memo: ``{key: iso-timestamp}``."""
    try:
        return key in _read_memo()
    except Exception:
        return False


def record_decline(key: str) -> None:
    """Memoize a muted tip so it is not offered again."""
    p = _decline_path()
    data = _read_memo()
    data[key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


_SHOWN_AT: dict[str, int] = {}
_LAST_SHOWN: Advice | None = None


def _in_cooldown(key: str, turn_index: int, cooldown_turns: int) -> bool:
    last = _SHOWN_AT.get(key)
    return last is not None and (turn_index - last) < cooldown_turns


def _mark_shown(advice: Advice, turn_index: int) -> None:
    global _LAST_SHOWN
    _SHOWN_AT[advice.key] = turn_index
    _LAST_SHOWN = advice


def last_shown() -> Advice | None:
    """The most recent tip rendered in this process (what ``/advisor mute`` mutes)."""
    return _LAST_SHOWN


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def _run_bounded(fn: Contributor, ctx: AdviceContext, timeout_s: float) -> Advice | None:
    """Call ``fn(ctx)`` on a daemon thread and wait at most ``timeout_s``.

    A contributor that overruns is abandoned: the thread is left to finish
    on its own and whatever it returns later is discarded. A contributor
    that raises yields ``None``.
    """
    box: list[Any] = []

    def _target() -> None:
        try:
            box.append(fn(ctx))
        except Exception as exc:
            log.debug("advisor contributor raised: %s", exc)
            box.append(None)

    worker = threading.Thread(target=_target, name="chat-advisor", daemon=True)
    worker.start()
    worker.join(max(timeout_s, 0.0))
    if worker.is_alive():
        log.debug("advisor contributor abandoned after %.0f ms", timeout_s * 1000)
        return None
    result = box[0] if box else None
    return result if isinstance(result, Advice) else None


def maybe_advise(
    ctx: AdviceContext,
    *,
    render: Callable[[str], None],
    budget_ms: int | None = None,
    cooldown_turns: int = COOLDOWN_TURNS,
    tty: bool | None = None,
) -> Advice | None:
    """Offer at most one tip for the turn described by ``ctx``.

    Runs the contributors in order inside one shared ``budget_ms``; the
    first ``Advice`` that is neither declined nor in cooldown is handed to
    ``render`` (the caller chooses the dim style for its surface) and
    returned. Everything else, including a failing ``render``, is swallowed
    and yields ``None``. This function only ever OFFERS text: it executes
    nothing, and ``Advice`` cannot carry a callable.

    ``tty`` overrides the ``_is_tty`` probe (tests and surfaces that know
    they are interactive).
    """
    try:
        if budget_ms is None:
            # Late-bound so tests (and operators) can move the module default;
            # a def-time default freezes the constant at import.
            budget_ms = DEFAULT_BUDGET_MS
        if not is_enabled():
            return None
        if not (_is_tty() if tty is None else tty):
            return None

        deadline = time.monotonic() + budget_ms / 1000.0
        for name, fn in _contributors():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            advice = _run_bounded(fn, ctx, remaining)
            if advice is None:
                continue
            if has_declined(advice.key):
                continue
            if _in_cooldown(advice.key, ctx.turn_index, cooldown_turns):
                continue
            try:
                render(advice.text)
            except Exception as exc:
                log.debug("advisor render failed (%s): %s", name, exc)
            _mark_shown(advice, ctx.turn_index)
            return advice
        return None
    except Exception as exc:
        log.debug("advisor hook failed: %s", exc)
        return None


def advise_turn(
    agent: Any,
    *,
    command: str,
    outcome: str,
    elapsed_ms: float,
    turn_index: int,
    render: Callable[[str], None],
    budget_ms: int | None = None,
) -> Advice | None:
    """Front-end helper: build the context from ``agent`` and call
    ``maybe_advise``. Both the REPL and the TUI call this at the end of a
    turn. Never raises."""
    try:
        session = getattr(agent, "session", None)
        session_id = str(getattr(session, "session_id", "") or getattr(session, "id", "") or "")
        tools = tuple(str(t) for t in (getattr(agent, "last_turn_tools", None) or ()))
        ctx = AdviceContext(
            command=(command or "")[:MAX_COMMAND_CHARS],
            outcome=outcome,
            elapsed_ms=int(elapsed_ms),
            session_id=session_id,
            turn_index=int(turn_index),
            tool_names=tools,
        )
    except Exception as exc:
        log.debug("advisor context unavailable: %s", exc)
        return None
    return maybe_advise(ctx, render=render, budget_ms=budget_ms)


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------


def _reset_for_tests(*, builtins: bool = False) -> None:
    """Clear the registry, the entry-point cache, the cooldown and the
    last-shown slot. ``builtins=True`` re-registers the shipped contributors."""
    global _BUILTINS_REGISTERED, _EP_CACHE, _LAST_SHOWN
    _REGISTRY.clear()
    _SHOWN_AT.clear()
    _LAST_SHOWN = None
    _EP_CACHE = None
    _BUILTINS_REGISTERED = False
    if builtins:
        _register_builtins()
    else:
        _BUILTINS_REGISTERED = True
