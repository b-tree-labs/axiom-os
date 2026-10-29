# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``approval.*`` — the human's side of the gate.

An action held for confirmation is only half a mechanism. The other half is a
person being able to see what is waiting and answer it, from a different
process than the one that asked, possibly hours later. These are the skills
that let them.

Registering them with specs is what projects one definition onto the CLI verb,
the MCP tool and the agent-facing function (ADR-072), so the same three
behaviours reach a terminal, ``neut chat`` and somebody's own harness without
a second implementation.

Which surfaces, and why it is the whole point
---------------------------------------------

``approval.pending`` declares all four surfaces. Asking what is waiting is
read-only, and an agent that can see its own blocked queue can say "I am
waiting on you" instead of stalling silently.

``approval.approve`` and ``approval.reject`` declare **cli only**. This is the
load-bearing line in the file. A gate whose approve verb is reachable from an
agent tool is not a gate: the agent proposes a write, the gate holds it, and
the agent calls approve. The declared-surfaces mechanism exists for bounded
exposure, and this is the case it was built for.

That constraint is not a comment. ``test_skills.py`` asserts these two never
carry ``agent_tool`` or ``mcp``, because the failure it guards against is a
one-word edit that looks like an improvement.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

__all__ = ["approve", "pending", "register_all", "reject"]

#: Surfaces a decision verb may be projected onto. See the module docstring:
#: an agent that can reach approve can approve itself.
_DECISION_SURFACES = ("cli", "skill_md")


def _brand_cli() -> str:
    """The command the operator actually typed.

    Hardcoding "axi" tells a consumer distribution's operator to run a command
    that does not exist on their machine. The CLI name follows branding; only
    the environment variable is a fixed literal, so a runbook can name it.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 — a label never takes the command down
        return "axi"


def _gate(ctx: SkillContext) -> ApprovalGate:
    """The durable gate, rooted in the context's state dir.

    Durable unconditionally here, unlike the library default. A verb typed at a
    terminal is by definition a different process from the one that proposed the
    action, so an in-memory store would show an empty queue every time and look
    like a working command with nothing to do.
    """
    return ApprovalGate(FileActionStore(ctx.state_dir / "orchestrator" / "approvals.json"))


def _who(ctx: SkillContext) -> str:
    """Who to record as having decided.

    ``ctx.actor`` when a dispatcher set one, otherwise the principal handle.
    Never blank: an unattributed approval is the thing this field exists to
    prevent, so an unknown decider is recorded as unknown rather than omitted.
    """
    if ctx.actor:
        return ctx.actor
    handle = getattr(ctx.principal, "handle", None)
    return str(handle) if handle else "@unknown:local"


def _render(action) -> dict[str, Any]:
    d = action.to_dict()
    return {k: d[k] for k in ("action_id", "name", "params", "status", "created_at")}


def pending(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """What is waiting on a human right now."""
    actions = _gate(ctx).pending()
    return SkillResult(
        ok=True,
        value={
            "count": len(actions),
            "pending": [_render(a) for a in actions],
        },
    )


def _decide(params: dict[str, Any], ctx: SkillContext, *, approving: bool) -> SkillResult:
    action_id = params.get("action_id")
    if not action_id:
        return SkillResult(ok=False, errors=["action_id is required"])

    gate = _gate(ctx)
    before = gate.get(str(action_id))
    if before is None:
        return SkillResult(
            ok=False,
            errors=[
                f"no action {action_id!r} in the queue. It may have been answered "
                "already, or purged after completing. Run "
                f"'{_brand_cli()} approve list'."
            ],
        )
    if before.status.value != "pending":
        # Deliberately not an error the caller has to parse: answering an
        # already-answered action is a race between two humans, not a mistake,
        # and the useful reply is what the standing answer is and who gave it.
        return SkillResult(
            ok=False,
            errors=[
                f"action {action_id} is already {before.status.value}"
                + (f", decided by {before.decided_by}" if before.decided_by else "")
            ],
            value={"action": _render(before)},
        )

    who = _who(ctx)
    if approving:
        after = gate.approve(str(action_id), decided_by=who)
    else:
        after = gate.reject(str(action_id), str(params.get("reason", "")), decided_by=who)

    return SkillResult(
        ok=True,
        value={"action": _render(after), "decided_by": who},
        actions_taken=[f"{'approved' if approving else 'rejected'} {action_id} as {who}"],
    )


def approve(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Approve one pending action. CLI only, on purpose."""
    return _decide(params, ctx, approving=True)


def reject(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Reject one pending action, with a reason. CLI only, on purpose."""
    return _decide(params, ctx, approving=False)


def register_all(registry: SkillRegistry) -> None:
    registry.register_skill(
        SkillSpec(
            name="approval.pending",
            fn=pending,
            description="List actions waiting on a human decision.",
            inputs={},
            side_effects=False,
            idempotent=True,
            surfaces=("cli", "mcp", "agent_tool", "skill_md"),
        ),
        mutating=False,
    )
    registry.register_skill(
        SkillSpec(
            name="approval.approve",
            fn=approve,
            description="Approve one pending action.",
            inputs={"action_id": "str"},
            side_effects=True,
            idempotent=False,
            surfaces=_DECISION_SURFACES,
        )
    )
    registry.register_skill(
        SkillSpec(
            name="approval.reject",
            fn=reject,
            description="Reject one pending action, with a reason.",
            inputs={"action_id": "str", "reason": "str"},
            side_effects=True,
            idempotent=False,
            surfaces=_DECISION_SURFACES,
        )
    )
