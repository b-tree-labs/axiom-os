# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/undo``: put back the files the agent's last turn(s) wrote, from the pre-images it kept (C16).

Safe by construction: a file changed after the agent wrote it blocks the undo (the user's later edit would
be lost) unless ``--force``; the current contents are snapshotted before anything is restored, so even a
forced undo loses nothing; and the undo is an action-ledger record of its own, which ``/undo`` reverses once.

``--force`` over a changed file is the destructive form, so it is plan / apply (``plan_apply``): the first
``/undo --force`` records the plan (the files, what the agent left, what is there now) as a held action and
changes nothing; ``/undo --force --apply <id>`` runs it only against an approval of exactly that plan, and
refuses if the files moved again, the plan expired, or it was already applied. The approval is the one
``axi approve`` answers; at the person's own terminal, ``--apply`` records that person's approval.
"""

from __future__ import annotations

from .. import checkpoints, plan_apply
from ..command_registry import command
from ..follow_ups import FollowUp, result

USAGE = "\n  Usage: /undo [n] [--turn N] [--force] [--apply <id>]\n"
KIND = "chat.undo.force"
_OWN_TERMINAL = frozenset({"repl", "tui"})

FOLLOW_UPS = {
    "redo": FollowUp("/undo", "undo this undo: put back what the agent wrote"),
    "diff": FollowUp("/diff", "see what the agent wrote and what changed since"),
    "force": FollowUp(
        "/undo --force", "plan the restore anyway; your current version is kept and nothing runs until you confirm"
    ),
    "apply": FollowUp("/undo --force --apply {approval_id}", "run the planned restore you just approved"),
    "context": FollowUp("/context", "see what the session now holds"),
}


def _short(h: str | None) -> str:
    return "absent" if h is None else h[:10]


def _parse(args: list[str]):
    n, turn, force, apply_id = 1, None, False, None
    it = iter(args)
    for a in it:
        if a == "--force":
            force = True
        elif a == "--apply":
            try:
                apply_id = next(it)
            except StopIteration:
                return None
        elif a == "--turn":
            try:
                turn = int(next(it))
            except (StopIteration, ValueError):
                return None
        elif a.isdigit() and int(a) > 0:
            n = int(a)
        else:
            return None
    return n, turn, force, apply_id


def _subject(sid: str, chosen, force_conflicts) -> dict:
    return {
        "session": sid,
        "turns": sorted(t.number for t in chosen),
        "conflicts": sorted(
            ({"path": c.path, "expected": c.expected, "actual": c.actual} for c in force_conflicts),
            key=lambda c: c["path"],
        ),
    }


def _choose(sid: str, turns, candidates, n: int, turn: int | None):
    """The turns to revert, or a sentence saying why none."""
    if turn is not None:
        match = next((t for t in turns if t.number == turn), None)
        if match is None:
            return None, f"\n  There is no turn {turn} with file changes in this session (1..{len(turns)}).\n"
        if match not in candidates:
            why = "a redo is final" if match.kind == checkpoints.KIND_REDO else "it is already reverted"
            return None, f"\n  Turn {turn} cannot be undone: {why}.\n"
        return [match], ""
    if not candidates:
        return None, (
            f"\n  Nothing to undo: the agent has no un-reverted file changes in this session.\n\n"
            f"  {checkpoints.CANNOT_UNDO}\n"
        )
    if n > len(candidates):
        return None, f"\n  Only {len(candidates)} turn(s) can be undone; asked for {n}.\n"
    return candidates[:n], ""


def _render_done(result_, conflicts) -> str:
    nums = ", ".join(str(x) for x in result_.turn_numbers)
    head = f"undid the earlier undo (turn {nums})" if result_.redo_of else f"undid turn {nums}"
    lines = ["", f"  {head}:"]
    lines += [f"    restored  {p}" for p in result_.restored]
    lines += [f"    removed   {p}  (the agent created it)" for p in result_.removed]
    if conflicts:
        lines.append(
            f"  forced over {len(conflicts)} file(s) changed since the agent wrote them; "
            "what was there is kept, and /undo reverses this."
        )
    lines += [
        "  Recorded in the action ledger as an undoable action.",
        "",
        f"  {checkpoints.CANNOT_UNDO}",
        "",
    ]
    return "\n".join(lines)


def _undo(ctx, args: list[str]):
    parsed = _parse(args)
    if parsed is None:
        return USAGE
    n, turn, force, apply_id = parsed
    sid = ctx.agent.session.session_id
    if apply_id:  # never a plain undo: --apply runs the planned step against its approval, or refuses
        return _apply_planned(ctx, sid, apply_id)
    turns = checkpoints.history(sid)
    candidates = checkpoints.undoable(turns)
    chosen, why = _choose(sid, turns, candidates, n, turn)
    if chosen is None:
        return why

    _, conflicts = checkpoints.plan_conflicts(sid, chosen)
    if conflicts and not force:
        lines = ["", "  Not undone: these files changed since the agent wrote it, and the undo would discard that:"]
        for c in conflicts:
            lines.append(f"    {c.path}  (agent left {_short(c.expected)}, now {_short(c.actual)})")
        lines += [
            "",
            "  Review with /diff. /undo --force plans the restore anyway: your current version is kept in the",
            "  checkpoint store and a following /undo brings it back.",
            "",
        ]
        return result("/undo", "\n".join(lines), "diff", "force")

    if conflicts:  # the destructive form: plan now, apply later against an approval of exactly this plan
        return _plan_force(ctx, sid, chosen, conflicts)

    try:
        done = checkpoints.undo(sid, chosen, tool="/undo")
    except (checkpoints.SnapshotRefused, FileNotFoundError, OSError) as exc:
        return f"\n  Nothing was changed. {type(exc).__name__}: {exc}\n"
    return result("/undo", _render_done(done, []), "redo", "diff")


def _plan_force(ctx, sid, chosen, conflicts):
    subject = _subject(sid, chosen, conflicts)
    principal = getattr(ctx.agent.session, "principal_id", "") or ""
    summary = f"restore {len(subject['turns'])} turn(s), overwriting {len(conflicts)} file(s) changed since"
    plan = plan_apply.propose(KIND, subject, summary=summary, requested_by=principal)
    lines = [
        "",
        f"  Planned, not run: {summary}.",
        *(f"    {c.path}  (agent left {_short(c.expected)}, now {_short(c.actual)})" for c in conflicts),
        f"  Plan {plan.plan_hash[:12]}, held as {plan.approval_id}, valid until {plan.expires_at}.",
        "  Your current version of each file is kept in the checkpoint store; /undo brings it back.",
        "",
    ]
    return result("/undo", "\n".join(lines), ("apply", {"approval_id": plan.approval_id}), "diff")


def _apply_planned(ctx, sid, apply_id):
    """Run the plan held under ``apply_id``. The turns are the plan's own, never re-chosen by this call."""
    action = _held(apply_id)
    if action is None or action.name != f"{plan_apply.ACTION_PREFIX}{KIND}":
        return f"\n  Nothing is held under {apply_id}. /undo --force makes a plan.\n"
    planned = action.params.get("subject") or {}
    if planned.get("session") != sid:
        return f"\n  Not run: plan {apply_id} was made in another session.\n"
    wanted = str(action.params.get("plan_hash") or "")
    numbers = set(planned.get("turns") or [])
    chosen = [t for t in checkpoints.history(sid) if t.number in numbers]
    principal = getattr(ctx.agent.session, "principal_id", "") or ""
    if ctx.surface in _OWN_TERMINAL and action.status.value == "pending":
        # The person at their own terminal typed --apply: that is their approval, recorded in the queue.
        plan_apply.approve_in_person(
            plan_apply.Plan(KIND, planned, wanted, apply_id, "", "", ""), principal or _local_handle()
        )

    def _recompute():
        _, now_conflicts = checkpoints.plan_conflicts(sid, chosen)
        return _subject(sid, chosen, now_conflicts)

    def _execute(_now):
        return checkpoints.undo(sid, chosen, tool="/undo")

    try:
        done = plan_apply.apply(wanted, apply_id, kind=KIND, recompute=_recompute, execute=_execute)
    except plan_apply.PlanRefused as exc:
        return f"\n  Not run: {exc}\n"
    except (checkpoints.SnapshotRefused, FileNotFoundError, OSError) as exc:
        return f"\n  Nothing was changed. {type(exc).__name__}: {exc}\n"
    return result("/undo", _render_done(done, planned.get("conflicts") or [True]), "redo", "diff")


def _held(action_id: str):
    try:
        return plan_apply._gate().get(action_id)  # noqa: SLF001 - the one queue, read the one way
    except Exception:  # noqa: BLE001
        return None


def _local_handle() -> str:
    try:
        from axiom.infra.principal import local_handle

        return local_handle()
    except Exception:  # noqa: BLE001
        return "@local:local"


command(
    "/undo",
    "Revert the agent's last file-writing turn (/undo [n] [--turn N] [--force])",
    "Agent",
    usage="/undo [n|--turn N] [--force] [--apply <id>]",
    mutates=True,
    risk="destructive",
    follow_ups=FOLLOW_UPS,
)(_undo)
