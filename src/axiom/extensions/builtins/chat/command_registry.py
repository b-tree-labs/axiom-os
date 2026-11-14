# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The slash command registry: one declaration per command (ADR-168).

Dispatch, tab-completion and ``/help`` all derive from a ``Command`` declared here, so a command cannot
be handled but not advertised, advertised but not handled, or listed in help but not completed. A command
may be limited to some surfaces (``repl``, ``tui``, ``headless``), and a surface can take over a command
with its own interaction (the TUI's session picker) while the text handler stays the shared fallback.

Handlers keep the contract the REPL always had: they return a ``str`` to print, the string ``"exit"``,
a ``UserCommandPrompt`` to route through the agent, or ``None``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

Surface = Literal["repl", "tui", "headless"]
ALL_SURFACES: frozenset[str] = frozenset({"repl", "tui", "headless"})


class _NotFound:
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "NOT_FOUND"


#: Returned by ``dispatch`` when no registered command matches, so the caller can try its other sources
#: (CLI namespaces, user-defined commands) without a handler having to say "not mine".
NOT_FOUND = _NotFound()


#: What a command may be exposed as. ``chat`` and ``cli`` are the defaults; ``mcp`` projects the command's
#: skill function as an ``axiom_chat__<verb>`` tool; ``harness-command-file`` is a generated command file for
#: another harness (declared here so the portability work has one place to read it from).
EXPOSURES: frozenset[str] = frozenset({"chat", "cli", "mcp", "harness-command-file"})
DEFAULT_EXPOSE: frozenset[str] = frozenset({"chat", "cli"})
#: How much harm a mistaken call can do. Only ``read`` and ``reversible-write`` may be exposed over MCP;
#: ``destructive`` is reached only through plan / apply (``plan_apply``).
RISKS: tuple[str, ...] = ("read", "reversible-write", "destructive")


@dataclass(frozen=True)
class McpForm:
    """One form of a command that survives outside the chat loop, projected as ``axiom_chat__<verb>``.

    A command can have forms of different risk (``/memory search`` reads, ``/memory forget`` redacts), so
    exposure is declared per form with that form's own ``risk``; a destructive form is never listed here.
    ``skill`` is the registered skill name (``chat.tools``); the slash handler and the MCP tool call the
    same function behind it.
    """

    skill: str
    form: str = ""  # the slash form it mirrors, e.g. "/memory search"
    risk: str = "read"  # read | reversible-write
    admin: bool = False  # mirrors requires="admin": exposed only when the MCP gate resolves an admin identity


@dataclass(frozen=True)
class CliSpec:
    """The command-line counterpart of a slash command, declared in the same registry entry.

    ``applies="startup"``: the flag is read when ``axi chat`` starts and runs ``line`` through the registry
    (the same handler the slash command uses) before the first prompt. ``applies="headless"``: the flag
    runs ``line``, prints the result and exits, with no chat started. ``line`` is a slash line;
    ``{values}`` is replaced by the flag's value(s). ``existing=True`` marks a flag the parser already had
    (``--model``): it is declared so help and the parity test see it, but not added twice.
    """

    flags: tuple[str, ...]
    line: str = ""
    applies: str = "startup"  # "startup" | "headless"
    metavar: str | tuple[str, ...] = ""  # "" = a switch; a tuple = that many values
    choices: tuple[str, ...] = ()
    help: str = ""
    existing: bool = False
    #: headless: the first value names a stored session the command runs against.
    session_arg: bool = False
    #: headless: the existing ``--json`` switch is forwarded to the handler as ``--json``.
    json: bool = False
    #: extra modifier flags forwarded to the handler: ``(("--format", ("md", "json")),)``.
    modifiers: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if self.applies not in ("startup", "headless"):
            raise ValueError(f"CliSpec.applies must be startup or headless, got {self.applies!r}")
        if not self.flags or not all(f.startswith("--") for f in self.flags):
            raise ValueError(f"a CliSpec flag starts with '--', got {self.flags!r}")

    @property
    def dest(self) -> str:
        return self.flags[0].lstrip("-").replace("-", "_")


@dataclass(frozen=True)
class CommandContext:
    agent: Any
    store: Any
    surface: str = "repl"
    input_prov: Any = None
    line: str = ""  # the raw slash line, for handlers that forward it


@dataclass(frozen=True)
class Command:
    name: str  # "/where"
    summary: str  # one line, shown in help and completion
    group: str  # help section
    handler: Callable[[CommandContext, list[str]], Any]
    usage: str = ""  # shown in help instead of the bare name, e.g. "/resume <id|#>"
    subcommands: Mapping[str, str] = field(
        default_factory=dict
    )  # completion: "/sessions rename" -> summary
    aliases: tuple[str, ...] = ()
    hidden: bool = False  # still dispatches; not in help or completion
    surfaces: frozenset[str] = ALL_SURFACES
    mutates: bool = False  # changes state outside the transcript (a headless caller may refuse it)
    requires: str = ""  # "admin": hidden from help and completion for others, and refused by dispatch
    #: Argument completion: ``complete(agent, args_so_far) -> [(value, description), ...]`` for the word being
    #: typed after the command (``args_so_far`` holds the finished words before it). None = no arguments to offer.
    complete: Callable[[Any, list[str]], list[tuple[str, str]]] | None = None
    #: The follow-ups this command can offer, declared once here as templates (``{name}`` placeholders are
    #: filled when a handler emits one). A registry test fails if any names a command or flag that does not
    #: exist. See ``follow_ups.py``.
    follow_ups: Mapping[str, Any] = field(default_factory=dict)
    #: The command-line counterpart, derived into ``axi chat`` flags and help from this one declaration.
    cli: CliSpec | None = None
    #: Where the command may be reached from. Default chat + cli.
    expose: frozenset[str] = DEFAULT_EXPOSE
    #: The riskiest form of the command: ``read`` | ``reversible-write`` | ``destructive``. Empty = derived
    #: from ``mutates``.
    risk: str = ""
    #: The forms projected over MCP (see ``McpForm``). Required when ``expose`` has ``mcp``.
    mcp: tuple[McpForm, ...] = ()

    def __post_init__(self) -> None:
        for n in (self.name, *self.aliases):
            if not n.startswith("/") or " " in n:
                raise ValueError(f"a command name must start with '/' and be one word, got {n!r}")
        risk = self.risk or ("reversible-write" if self.mutates else "read")
        if risk not in RISKS:
            raise ValueError(f"{self.name}: risk must be one of {RISKS}, got {risk!r}")
        object.__setattr__(self, "risk", risk)
        expose = frozenset(self.expose)
        if not expose <= EXPOSURES:
            raise ValueError(f"{self.name}: unknown exposure {sorted(expose - EXPOSURES)}")
        object.__setattr__(self, "expose", expose)
        if "mcp" in expose and not self.mcp:
            raise ValueError(f"{self.name}: an MCP-exposed command names the forms it exposes")
        for form in self.mcp:
            if form.risk not in ("read", "reversible-write"):
                raise ValueError(
                    f"{self.name}: {form.form or form.skill} is {form.risk}; only read and reversible-write "
                    "forms are exposed over MCP (a destructive one goes through plan / apply)"
                )
            if "." not in form.skill:
                raise ValueError(f"{self.name}: skill {form.skill!r} must be qualified (namespace.verb)")


def _group_order(first: list[str], name: str) -> int:
    return first.index(name) if name in first else len(first)


class CommandRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._order: list[Command] = []
        self._group_order: list[str] = []

    # ---- declaration ----

    def register(self, command: Command) -> Command:
        for n in (command.name, *command.aliases):
            if n.lower() in self._by_name:
                raise ValueError(f"{n} is already registered")
        for n in (command.name, *command.aliases):
            self._by_name[n.lower()] = command
        self._order.append(command)
        if command.group not in self._group_order:
            self._group_order.append(command.group)
        return command

    def set_completer(self, name: str, fn: Callable[[Any, list[str]], list[tuple[str, str]]]) -> None:
        """Attach an argument completer to a registered command (the Command is frozen, so it is replaced)."""
        old = self.get(name)
        if old is None:
            raise KeyError(name)
        new = replace(old, complete=fn)
        for n in (old.name, *old.aliases):
            self._by_name[n.lower()] = new
        self._order[self._order.index(old)] = new

    # ---- lookup ----

    def get(self, name: str) -> Command | None:
        return self._by_name.get(name.lower())

    def commands(self, *, include_hidden: bool = False, admin: bool = False) -> list[Command]:
        """The commands a viewer is shown. An admin-only command is listed only for an admin (or when the
        caller asks for everything with ``include_hidden``)."""
        return [
            c
            for c in self._order
            if include_hidden or (not c.hidden and (admin or c.requires != "admin"))
        ]

    def names(self) -> set[str]:
        """Every name that dispatches, including aliases and hidden commands."""
        return set(self._by_name)

    # ---- the three derived views ----

    def meta(self, *, admin: bool = False) -> dict[str, str]:
        """``{"/name": summary, "/name sub": summary}`` for completion and the unknown-command check."""
        out: dict[str, str] = {}
        for c in self.commands(admin=admin):
            out[c.name] = c.summary
            for sub, text in c.subcommands.items():
                out[f"{c.name} {sub}"] = text
        return out

    def help_text(self, *, admin: bool = False) -> str:
        """Help generated from the declarations, grouped in the order the groups were first declared."""
        lines: list[str] = []
        for group in self._group_order:
            members = [c for c in self.commands(admin=admin) if c.group == group]
            if not members:
                continue
            from axiom.infra.cli_format import Column, table

            lines.append(group)
            lines.extend(
                table(
                    [(c.usage or c.name, c.summary) for c in members],
                    [Column(), Column(wrap=True)],
                )
            )
            lines.append("")
        return "\n".join(lines).rstrip("\n")

    # ---- dispatch ----

    def dispatch(self, line: str, ctx: CommandContext) -> Any:
        parts = line.split()
        if not parts:
            return NOT_FOUND
        command = self.get(parts[0])
        if command is None:
            return NOT_FOUND
        if command.requires == "admin":
            from .roles import is_admin

            # The real control. Hiding the command from the list is a courtesy; a person who types it
            # anyway gets exactly what they get for a command that does not exist.
            if not is_admin(ctx.agent, surface=ctx.surface):
                return NOT_FOUND
        if ctx.surface not in command.surfaces:
            where = ", ".join(sorted(command.surfaces))
            return (
                f"\n  {command.name} is not available in the {ctx.surface}; it works in: {where}.\n"
            )
        try:
            out = command.handler(replace(ctx, line=line), parts[1:])
        except Exception as exc:  # noqa: BLE001 - a broken command must not end the chat
            return f"\n  {command.name} failed: {type(exc).__name__}: {exc}\n"
        _remember_follow_ups(ctx.agent, out)
        return out


def _remember_follow_ups(agent: Any, out: Any) -> None:
    """Keep the latest follow-ups on the agent for the autocomplete engine. A result without any clears them,
    so a stale suggestion never outlives the result it belonged to."""
    if agent is None or out == "exit":
        return
    try:
        agent._last_follow_ups = tuple(getattr(out, "follow_ups", ()) or ())
    except Exception:  # noqa: BLE001 - an agent that refuses attributes just gets no ghost text
        pass


#: The registry the chat surfaces use.
REGISTRY = CommandRegistry()


def command(
    name: str,
    summary: str,
    group: str,
    *,
    registry: CommandRegistry | None = None,
    **fields: Any,
):
    """Decorator: register ``handler(ctx, args)`` as a slash command."""

    def deco(handler: Callable[[CommandContext, list[str]], Any]):
        (registry or REGISTRY).register(Command(name, summary, group, handler, **fields))
        return handler

    return deco
