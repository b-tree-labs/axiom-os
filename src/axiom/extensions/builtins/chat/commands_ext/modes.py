# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/plan, /ask, /agent and /mode: the interaction mode by command (ADR-168).

Shift+Tab in the TUI cycled the mode; these make it reachable from every surface and say, each time, what
the mode really enforces. The text below is the code's behaviour, not an aspiration: see
``ChatAgent.turn`` (tool surface) and ``agent._mode_refusal`` (dispatch).
"""

from __future__ import annotations

from ..command_registry import CliSpec, CommandContext, command
from ..follow_ups import FollowUp, result

G_MODE = "Agent"

#: What each mode guarantees, as implemented. Kept next to the commands so a change to the agent's
#: behaviour has one obvious place to be reflected, and tests pin the load-bearing phrases.
GUARANTEES: dict[str, tuple[str, ...]] = {
    "ask": (
        "No tools are offered to the model: the request carries no tool list, so this is plain Q&A.",
        "A tool call that arrives anyway (a provider that ignores the omission, or a call written as "
        "text) is refused at dispatch and not run.",
    ),
    "plan": (
        "Tools are stripped from the request and a PLAN MODE instruction is added: the model is told to "
        "answer with a plan, naming the tools it would use.",
        "A tool call that arrives anyway is refused at dispatch unless the tool is classified read-only; "
        "mutating calls are never run.",
        "This is not a sandbox: a read-only-classified tool can still run, and the refusal trusts the "
        "tool's own classification.",
    ),
    "agent": (
        "Tools are offered and the model runs the full tool loop; writes still go through the approval "
        "gate and your /permissions.",
    ),
}


FOLLOW_UPS = {
    "ask": FollowUp("/ask", "plain Q&A: no tools offered"),
    "plan": FollowUp("/plan", "plan only: tools withheld, mutating calls refused"),
    "agent": FollowUp("/agent", "full tool loop; writes still need approval"),
    "tools": FollowUp("/tools", "see what the agent can call in agent mode, and each tool's permission"),
    "mode": FollowUp("/mode", "show what the current mode enforces"),
}
_BACK = {"ask": ("agent", "plan"), "plan": ("agent", "ask"), "agent": ("plan", "ask")}


def describe(mode: str, *, changed: bool) -> str:
    verb = "Mode set to" if changed else "Mode is"
    lines = [f"\n  {verb} {mode}."]
    lines += [f"    - {g}" for g in GUARANTEES[mode]]
    lines.append("    Switch with /ask, /plan or /agent.\n")
    return "\n".join(lines)


def _switch(mode: str):
    def handler(ctx: CommandContext, args: list[str]):
        ctx.agent.set_interaction_mode(mode)
        keys = list(_BACK[mode]) + (["tools"] if mode == "agent" else [])
        return result(f"/{mode}", describe(mode, changed=True), *keys)

    handler.__name__ = f"_{mode}"
    return handler


command(
    "/ask", "Plain Q&A: no tools offered", G_MODE, follow_ups=FOLLOW_UPS,
    cli=CliSpec(("--no-tools",), line="/ask", help="start in plain Q&A: no tools offered (--ask is the one-shot question)"),
)(_switch("ask"))
command(
    "/plan", "Plan only: tools withheld, mutating calls refused", G_MODE, follow_ups=FOLLOW_UPS,
    cli=CliSpec(("--plan",), line="/plan", help="start in plan mode"),
)(_switch("plan"))
command(
    "/agent", "Full tool loop (writes need approval)", G_MODE, follow_ups=FOLLOW_UPS,
    cli=CliSpec(("--agent",), line="/agent", help="start in the full tool loop (the default)"),
)(_switch("agent"))


@command("/mode", "Show the interaction mode and what it enforces", G_MODE, follow_ups=FOLLOW_UPS)
def _mode(ctx: CommandContext, args: list[str]):
    mode = ctx.agent._interaction_mode
    return result("/mode", describe(mode, changed=False), *_BACK[mode])
