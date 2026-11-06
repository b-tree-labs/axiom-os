# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi chat`` flags derived from the slash command registry (ADR-168).

A command's command-line counterpart is declared in the same registry entry as the command
(``Command.cli``), so the flag, the slash command and its help come from one declaration and cannot drift:

* ``startup`` flags (``--rag off``, ``--plan``, ``--trace``) run the command's slash line through the
  registry, with the same handler, just before the first prompt;
* ``headless`` flags (``--where``, ``--stats <session>``, ``--tools``) run the slash line, print the result
  and exit without starting a chat; ``--json`` is forwarded to the handler;
* a flag the parser already had (``--model``, ``--renew``) is declared with ``existing=True`` so ``--help``
  and the parity test see it, and is not added twice.

Nothing here knows a command by name. It reads ``REGISTRY`` and calls ``REGISTRY.dispatch``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .command_registry import REGISTRY, CliSpec, Command, CommandContext, CommandRegistry


def cli_commands(registry: CommandRegistry = REGISTRY) -> list[tuple[Command, CliSpec]]:
    from .builtin_commands import ensure_loaded

    ensure_loaded()
    return [(c, c.cli) for c in registry.commands(include_hidden=True) if c.cli is not None]


def _metavar_text(spec: CliSpec) -> str:
    if spec.choices:
        return "|".join(spec.choices)
    if isinstance(spec.metavar, tuple):
        return " ".join(spec.metavar)
    return spec.metavar


def mirror_lines(registry: CommandRegistry = REGISTRY) -> list[str]:
    """One line per declared flag: the flag, its value, and the slash command it mirrors."""
    from axiom.infra.cli_format import Column, table

    rows = []
    for cmd, spec in cli_commands(registry):
        flags = ", ".join(spec.flags)
        left = f"{flags} {_metavar_text(spec)}".strip()
        rows.append((left, f"mirrors {cmd.name}", spec.help or ""))
    return table(rows, [Column(), Column(), Column(wrap=True)])


def add_registry_flags(parser: argparse.ArgumentParser, registry: CommandRegistry = REGISTRY) -> None:
    """Add every declared flag the parser does not already have, and list them all in the help epilog."""
    existing = set(parser._option_string_actions)  # noqa: SLF001 - argparse has no public listing
    group = parser.add_argument_group("slash command mirrors (each does what the named slash command does)")
    modifiers: dict[str, tuple[str, ...]] = {}
    for cmd, spec in cli_commands(registry):
        for name, choices in spec.modifiers:
            modifiers[name] = choices
        if spec.existing:
            missing = [f for f in spec.flags if f not in existing]
            if missing:
                raise ValueError(f"{cmd.name}: declared existing flag(s) {missing} the parser does not have")
            continue
        clash = [f for f in spec.flags if f in existing]
        if clash:
            raise ValueError(f"{cmd.name}: {clash} is already a flag; declare it existing=True or rename it")
        kwargs: dict[str, Any] = {"dest": spec.dest, "help": f"{spec.help} (mirrors {cmd.name})"}
        if isinstance(spec.metavar, tuple):
            kwargs.update(nargs=len(spec.metavar), metavar=spec.metavar)
        elif spec.choices:
            kwargs.update(choices=spec.choices, metavar="|".join(spec.choices))
        elif spec.metavar:
            kwargs.update(metavar=spec.metavar)
        else:
            kwargs.update(action="store_true")
        group.add_argument(*spec.flags, **kwargs)
        existing.update(spec.flags)
    for name, choices in modifiers.items():
        if name in existing:
            continue
        group.add_argument(name, dest=name.lstrip("-").replace("-", "_"), choices=choices or None,
                           metavar="|".join(choices) if choices else "VALUE",
                           help="modifier for the flags above that take it (--export)")
        existing.add(name)
    parser.epilog = "slash command mirrors:\n" + "\n".join(mirror_lines(registry))
    parser.formatter_class = argparse.RawDescriptionHelpFormatter


def _value(args: argparse.Namespace, spec: CliSpec) -> Any:
    return getattr(args, spec.dest, None)


def _active(args: argparse.Namespace, registry: CommandRegistry, applies: str):
    for cmd, spec in cli_commands(registry):
        if spec.existing or spec.applies != applies:
            continue
        value = _value(args, spec)
        if value in (None, False, [], ()):
            continue
        yield cmd, spec, value


def line_for(spec: CliSpec, value: Any, args: argparse.Namespace | None = None) -> str:
    """The slash line a flag stands for. ``{values}`` is the flag's value(s) joined by spaces."""
    if isinstance(value, (list, tuple)):
        values = " ".join(str(v) for v in value)
    elif value is True:
        values = ""
    else:
        values = str(value)
    line = spec.line.format(values=values).strip()
    if not line:
        return ""
    for name, _choices in spec.modifiers:
        chosen = getattr(args, name.lstrip("-").replace("-", "_"), None) if args is not None else None
        if chosen:
            line += f" {name} {chosen}"
    if spec.json and args is not None and getattr(args, "json", False):
        line += " --json"
    return line


def _surface_for(cmd: Command, preferred: str) -> str:
    return preferred if preferred in cmd.surfaces else ("repl" if "repl" in cmd.surfaces else sorted(cmd.surfaces)[0])


def conflicts(args: argparse.Namespace, registry: CommandRegistry = REGISTRY) -> str:
    """A sentence when mode flags are combined (``--plan --agent``), else ''."""
    modes = [spec.flags[0] for cmd, spec, _ in _active(args, registry, "startup") if cmd.group == "Agent" and cmd.name in ("/plan", "/agent", "/ask")]
    return f"choose one of {', '.join(modes)}" if len(modes) > 1 else ""


def apply_startup(
    args: argparse.Namespace, agent: Any, store: Any, registry: CommandRegistry = REGISTRY
) -> list[str]:
    """Run each active startup flag's slash line through the registry. Returns what they printed."""
    out: list[str] = []
    for cmd, spec, value in _active(args, registry, "startup"):
        line = line_for(spec, value, args)
        if not line:
            continue  # a flag with no slash line (--no-memory) is applied by the entry point itself
        # The person at their own terminal started this process, so it runs as the REPL surface.
        result = registry.dispatch(line, CommandContext(agent=agent, store=store, surface="repl"))
        if isinstance(result, str) and result.strip():
            out.append(result)
    return out


def headless_request(
    args: argparse.Namespace, registry: CommandRegistry = REGISTRY
) -> tuple[Command, CliSpec, str, str] | None:
    """``(command, spec, session_id, line)`` for the headless flag in ``args``, or None."""
    active = list(_active(args, registry, "headless"))
    if not active:
        return None
    cmd, spec, value = active[0]
    session_id = ""
    if spec.session_arg:
        session_id = value[0] if isinstance(value, (list, tuple)) else str(value)
        value = True
    return cmd, spec, session_id, line_for(spec, value, args)


def _run_read_skill(cmd, session_id, out) -> int | None:
    """The ``--json`` form of a command that has a registered read skill goes through the same dispatch
    chokepoint the MCP tool uses (authority gate, audit, telemetry), so the flag and the tool cannot
    differ. None when the command has no such skill, so the caller uses the slash handler."""
    import json
    import logging

    form = next((f for f in getattr(cmd, "mcp", ()) if not f.form), None)
    if form is None:
        return None
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import invoke_capability
    from axiom.infra.skills import SkillContext

    from .skills import bind_default

    registry = bind_default()
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.chat"))
    params = {"session": session_id} if session_id else {}
    result = invoke_capability(registry, form.skill, params, ctx, surface="cli")
    if not result.ok:
        print(getattr(result, "error", None) or "the command failed", file=sys.stderr)
        return 1
    print(json.dumps(result.value, indent=2, default=str), file=out)
    return 0


def run_headless(
    args: argparse.Namespace,
    registry: CommandRegistry = REGISTRY,
    *,
    agent_factory=None,
    out=None,
) -> int | None:
    """Run the headless flag, print its result, and return an exit code; None when no headless flag was given."""
    request = headless_request(args, registry)
    if request is None:
        return None
    cmd, spec, session_id, line = request
    out = out or sys.stdout
    if agent_factory is None:
        from .skills.read import agent_for

        def agent_factory(sid):  # noqa: E306
            return agent_for({"session": sid})

    agent, err = agent_factory(session_id)
    if err or agent is None:
        print(err or "could not build a chat agent", file=sys.stderr)
        return 1
    if "--json" in line.split():
        done = _run_read_skill(cmd, session_id, out)
        if done is not None:
            return done
    surface = _surface_for(cmd, "headless")
    result = registry.dispatch(line, CommandContext(agent=agent, store=getattr(agent, "session_store", None), surface=surface))
    text = result if isinstance(result, str) else ""
    if not text.strip():
        print(f"{cmd.name} produced no output here.", file=sys.stderr)
        return 1
    print(text.strip("\n"), file=out)
    return 0
