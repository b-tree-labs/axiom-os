# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reconcile what is REGISTERED against what is DECLARED, and teach the gap.

A capability registered with ``registry.register(name, fn)`` works perfectly
from the CLI and is invisible to everything else: no description, no declared
surfaces, so it cannot appear in an MCP tool list, the agent tool loop,
``SKILL.md`` or the capability discovery block. Half the platform arrived there
— 58 of 117 capabilities — because that is the shorter form and the one most
examples use.

A ratchet on the count stops it growing and teaches nobody, so the next author
writes the same line. This is the other half: it finds the undeclared ones,
**writes the declaration for you**, and says what promoting each one buys in
concrete terms — the tool name it would get, the surfaces it would reach, the
gates it becomes eligible for.

Three rules it does not bend:

**It never applies anything.** Editing somebody's extension because a heuristic
inferred a description is the same drift arriving from the other direction. It
proposes; a human approves.

**It never invents a description.** An undocumented capability is reported as
blocked on one rather than given "Run demo.mystery" — a line that teaches
nothing, looks authored, and would never be fixed because it no longer looks
broken.

**It never proposes MCP for a mutating verb.** A write reaching a protocol
surface is a decision somebody makes, not a default a tool hands out, so
accepting every suggestion verbatim can never widen an effect nobody chose.
"""

from __future__ import annotations

import inspect
from typing import Any

#: Verb names that usually mean "reads something". Used ONLY to raise a hint,
#: never to widen a proposal: `registry.register(name, fn)` defaults to
#: ``mutating=True``, so every legacy verb is proposed CLI-only — correct, and
#: usually wrong, which buries the capabilities that could reach every surface
#: today. Guessing from a name and handing out an MCP surface is how a write
#: ends up on a protocol, so the hint asks the author instead.
_READ_SHAPED = (
    "status", "list", "ls", "show", "get", "audit", "stat", "diagnose",
    "discover", "check", "describe", "report", "verify", "resolve",
)

#: Surfaces a read-only capability can safely reach all at once.
READ_SURFACES = ("cli", "mcp", "agent_tool")
#: A mutating capability starts CLI-only; widening it is a per-verb decision.
WRITE_SURFACES = ("cli",)


def _first_sentence(fn: Any) -> str:
    """The capability's own docstring summary, or "" when it has none."""
    doc = inspect.getdoc(fn) or ""
    line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return line


def _tool_name(capability: str) -> str:
    """What this capability would be called on MCP (ADR-073 projection)."""
    namespace, _, verb = capability.partition(".")
    return f"axiom_{namespace}__{verb}".replace("-", "_")


def _gains(capability: str, *, mutating: bool) -> list[str]:
    """What declaring this capability actually buys, in concrete terms.

    Not "conformance". Nobody acts on conformance. A tool name they can call
    from their editor, and the gates the capability becomes eligible for, are
    things an author can picture.
    """
    gains = [
        "Capability telemetry: invocations land in the capability series, so "
        "usage (and non-usage) of this verb becomes measurable.",
        "Site authority rules (ADR-114) can name it, so an operator can permit "
        "or restrict it per principal and per surface.",
        "`SKILL.md` generation picks up the description instead of emitting a "
        "stub that points back at the function.",
    ]
    if mutating:
        gains.insert(
            0,
            "A declared effect means every surface gates it identically — the "
            "CLI confirm, the chat approval gate and the MCP side-effect flag "
            "all read the same declaration instead of each guessing.",
        )
    else:
        gains.insert(
            0,
            f"Callable as `{_tool_name(capability)}` from any MCP client "
            f"(Claude Code, Cursor, chat) and from the agent tool loop — not "
            f"just from a terminal.",
        )
        gains.insert(
            1,
            "Appears in the capability discovery block until somebody uses it, "
            "so an assistant can learn it exists instead of reaching for a "
            "shell equivalent.",
        )
    return gains


def _proposal(capability: str, fn: Any, *, mutating: bool) -> dict[str, Any]:
    description = _first_sentence(fn)
    surfaces = WRITE_SURFACES if mutating else READ_SURFACES
    proposal: dict[str, Any] = {
        "name": capability,
        # The REAL callable, not the verb. Deriving this from the name produced
        # `fn=list` for `schedule.list` — the Python builtin. That pastes,
        # compiles and registers the wrong thing, which is worse than a
        # proposal that fails loudly, because it looks right.
        "fn_name": getattr(fn, "__name__", "") or capability.partition(".")[2],
        "description": description,
        "side_effects": bool(mutating),
        "idempotent": not mutating,
        "surfaces": list(surfaces),
        "tool_name": None if mutating else _tool_name(capability),
        "gains": _gains(capability, mutating=mutating),
        "blocked_on": [],
        "why_not_mcp": (
            "declared as having an effect; putting a write on a protocol "
            "surface is a per-verb decision, not a default"
        ) if mutating else "",
        "hint": "",
    }
    verb = capability.partition(".")[2]
    if mutating and verb in _READ_SHAPED:
        proposal["hint"] = (
            f"`{verb}` looks like it reads rather than writes. "
            f"`registry.register(name, fn)` defaults to mutating=True, so this "
            f"is proposed CLI-only. If it has no side effects, register it with "
            f"`mutating=False` and declare `surfaces=(\"cli\", \"mcp\", "
            f"\"agent_tool\")` — it becomes `{_tool_name(capability)}`, "
            f"callable from any MCP client and offered in the discovery block. "
            f"If it does write, leave it as it is; this is a question, not a "
            f"correction."
        )
    if not description:
        proposal["blocked_on"].append(
            "no docstring: add a one-line summary to the skill function. It "
            "becomes the description on every surface, so it is the sentence "
            "someone reads when deciding whether to call this."
        )
    return proposal


def promotion_report(registry: Any) -> dict[str, Any]:
    """Which capabilities are declared, which are not, and the fix for each."""
    names = sorted(getattr(registry, "_skills", {}).keys())
    promoted: list[str] = []
    unpromoted: list[str] = []
    proposals: dict[str, Any] = {}

    for name in names:
        if registry.spec(name) is not None:
            promoted.append(name)
            continue
        unpromoted.append(name)
        proposals[name] = _proposal(
            name,
            registry._skills[name],
            mutating=bool(getattr(registry, "_mutating", {}).get(name, True)),
        )

    return {
        "total": len(names),
        "promoted": promoted,
        "unpromoted": unpromoted,
        "proposals": proposals,
        "clean": not unpromoted,
        # Stated in the payload, not just enforced in code, so a caller reading
        # this over a protocol knows it is looking at a proposal.
        "applied": [],
        "requires_human": True,
    }


def proposed_spec_source(proposal: dict[str, Any]) -> str:
    """The `SkillSpec(...)` to paste into the extension's `skills/__init__.py`.

    Paste-ready on purpose. A finding that leaves the author to write the fix
    gets deferred; the point of a crutch is that it carries you.
    """
    surfaces = ", ".join(f'"{s}"' for s in proposal["surfaces"])
    description = proposal["description"] or "TODO: one line — what does this do?"
    return (
        "SkillSpec(\n"
        f'    name="{proposal["name"]}",\n'
        f"    fn={proposal.get('fn_name') or proposal['name'].partition('.')[2]},\n"
        f'    description="{description}",\n'
        f"    side_effects={proposal['side_effects']},\n"
        f"    idempotent={proposal['idempotent']},\n"
        f"    surfaces=({surfaces},),\n"
        ")"
    )


def render(report: dict[str, Any], *, limit: int = 5) -> list[str]:
    """Human-readable lines for the CLI and the heartbeat digest."""
    if report["clean"]:
        return [f"all {report['total']} capabilities are declared"]

    hinted = sum(1 for p in report["proposals"].values() if p["hint"])
    lines = [
        f"{len(report['unpromoted'])} of {report['total']} capabilities are "
        f"registered but NOT declared — nothing can discover them:",
        "",
    ]
    if hinted:
        lines[0] += (
            f"\n  ({hinted} of them look read-only and could reach MCP today "
            f"— see the hints)"
        )
    for name in report["unpromoted"][:limit]:
        proposal = report["proposals"][name]
        target = proposal["tool_name"] or "cli only (declared effect)"
        lines.append(f"  {name}  ->  {target}")
        if proposal["hint"]:
            lines.append(f"      hint:  {proposal['hint']}")
        if proposal["blocked_on"]:
            lines.append(f"      needs: {proposal['blocked_on'][0]}")
    remaining = len(report["unpromoted"]) - limit
    if remaining > 0:
        lines.append(f"  ...and {remaining} more")
    lines += [
        "",
        "What declaring one buys:",
    ]
    sample = report["proposals"][report["unpromoted"][0]]
    lines += [f"  - {gain}" for gain in sample["gains"]]
    lines += [
        "",
        "Nothing was changed. See the proposed SkillSpec for each with --json, "
        "or promote one by pasting it into that extension's skills/__init__.py.",
    ]
    return lines


__all__ = ["promotion_report", "proposed_spec_source", "render",
           "READ_SURFACES", "WRITE_SURFACES"]


def bound_registry() -> Any:
    """A registry populated from every installed extension's ``skills`` package.

    Deliberately a FRESH registry, never the process-global default: this is an
    observation about the install and must not mutate what the caller is using.

    It has to bind every extension, not just the caller's — the whole failure
    was that no single extension could see the fleet-wide gap, so nobody did.
    """
    import importlib
    import pkgutil

    from axiom.infra.skills import SkillRegistry

    import axiom.extensions.builtins as builtins_pkg

    registry = SkillRegistry()
    for mod_info in pkgutil.iter_modules(builtins_pkg.__path__):
        try:
            mod = importlib.import_module(
                f"axiom.extensions.builtins.{mod_info.name}.skills"
            )
        except Exception:  # noqa: BLE001 — an extension that will not import
            continue
        binder = getattr(mod, "bind", None)
        if callable(binder):
            try:
                binder(registry)
            except Exception:  # noqa: BLE001 — one bad binder must not hide the rest
                continue
    return registry


def run(params: dict[str, Any], ctx: Any = None) -> Any:
    """``axi hygiene stat capabilities`` — the declaration-drift check."""
    from axiom.infra.skills import SkillResult

    report = promotion_report(bound_registry())
    for name, proposal in report["proposals"].items():
        proposal["spec_source"] = proposed_spec_source(proposal)
    return SkillResult(
        # Undeclared capabilities make this red. A green drift check beside half
        # the platform being unfindable is the state that let it get to half.
        ok=report["clean"],
        value=report,
        actions_taken=render(report),
        errors=[] if report["clean"] else [
            f"{len(report['unpromoted'])} capabilities are registered but not "
            f"declared, so nothing can discover them"
        ],
    )
