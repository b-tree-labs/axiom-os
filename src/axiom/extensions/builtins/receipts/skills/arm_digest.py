# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``receipts.arm_digest`` — put the digest on its declared cadence.

Stage 0 of the oversight journey is "something reaches a person who is not
looking at this", and walking it found the chain broken in five places. Four
are fixed. This verb closes the one that turns a composable message into a
recurring one.

Deliberately a separate verb from ``receipts.digest``. Sending and arming
are different acts with different reach radii — one interrupts somebody
once, the other commits to interrupting them for ever — and folding the
second into the first would mean no way to send a digest without also
scheduling one.

The cadence comes from the declared audience rather than from this
extension's manifest. ``[[extension.schedule]]`` exists and would work, but
it would bake one cadence into the software for every install, and 07:00
daily is a choice about somebody's morning rather than a property of the
code. The backup policy settled this shape already: the policy owns the
schedule string, and a verb reconciles PULSE against it.

**What this verb cannot do, and says so.** Registering a cadence is not the
same as anything firing it. PULSE's tick loop lives in the
``data_platform_orchestrator`` service, declared with
``deployment_profile = "server"`` — so on a node where that service is not
running, an armed cadence is an inert row. That requirement is stated in
every armed result rather than left for somebody to infer from the silence,
because a verb that returns "armed" while nothing will fire is the same
false green the rest of this stage was fixed for.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: The qualified skill name PULSE will invoke.
ACTION = "receipts.digest"

#: The service whose tick loop actually fires a registered cadence.
PULSE_HOST = "data_platform_orchestrator"

#: Stated in every armed result. A requirement, not a warning: a cadence is
#: a row until something reads it, and the thing that reads it is a service
#: with a server deployment profile. Checked by a test, so the sentence
#: cannot go on naming a host that has been renamed or removed.
NEEDS_A_HOST = (
    f"a registered cadence only fires where PULSE's tick loop runs — the "
    f"{PULSE_HOST} service (deployment_profile = server). On a node without "
    "it this cadence is an inert row, so check that it is running there"
)


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Reconcile PULSE against the declared audience.

    Params:
      - ``dry_run`` (bool): report what would change without changing it.

    Registers the cadence when absent, reschedules when the declared string
    has changed, resumes a paused one, and pauses when the declaration is
    disabled. Idempotent: running it twice changes nothing the second time.
    """
    from ..audience import audience_path, load_audience, no_audience_declared, validate_audience

    dry_run = bool(params.get("dry_run"))
    declared = load_audience()
    if declared is None:
        return SkillResult(ok=False, errors=[no_audience_declared()])

    problems = validate_audience(declared)
    if problems:
        return SkillResult(ok=False, errors=problems)

    try:
        from axiom.extensions.builtins.schedule import api as pulse_api
        from axiom.extensions.builtins.schedule import store as pulse_store
        from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition
        from axiom.extensions.builtins.schedule.formats import parse as parse_cadence
    except Exception as exc:  # noqa: BLE001 — the extension may be absent
        return SkillResult(
            ok=False,
            errors=[f"the schedule extension is not available, so nothing can be armed: {exc}"],
        )

    def _rows() -> list:
        with pulse_store.session_scope() as s:
            rows = (
                s.query(ScheduleDefinition)
                .filter(ScheduleDefinition.action == ACTION)
                .filter(ScheduleDefinition.state != "cancelled")
                .order_by(ScheduleDefinition.created_at)
                .all()
            )
            for r in rows:
                s.expunge(r)
            return rows

    try:
        existing = _rows()
    except Exception as exc:  # noqa: BLE001 — no PULSE database yet
        return SkillResult(
            ok=False,
            errors=[f"cannot read the schedule store, so nothing can be armed: {exc}"],
        )

    if not declared.enabled:
        paused = []
        for row in existing:
            if row.state == "active" and not dry_run:
                pulse_api.pause(pulse_api.ScheduleId(row.id), "digest audience disabled")
            paused.append(row.id)
        return SkillResult(
            ok=True,
            value={"armed": False, "paused": paused, "dry_run": dry_run},
            actions_taken=[
                f"the declared audience is disabled; paused {len(paused)} cadence(s)"
                if paused
                else "the declared audience is disabled and no cadence was armed"
            ],
        )

    cadence = parse_cadence(declared.schedule)
    # The params PULSE will hand back to receipts.digest. Recipients are
    # deliberately NOT baked in here: the digest reads the declaration at
    # fire time, so editing who it reaches does not mean re-arming.
    envelope = {"action": ACTION, "params": {}}

    if not existing:
        if dry_run:
            return SkillResult(
                ok=True,
                value={"armed": False, "would_register": declared.schedule, "dry_run": True},
                actions_taken=[f"would register {ACTION} on '{declared.schedule}'", NEEDS_A_HOST],
            )
        sid = pulse_api.register(
            envelope=envelope,
            cadence=cadence,
            action=ACTION,
            description=f"Arrival digest @ {declared.schedule}",
            extension="receipts",
            retry_policy={"max_attempts": 1},
            # fire_once: a digest that was missed while the node was down
            # should arrive once, not N times for N skipped periods. Alert
            # fatigue is the thing this whole surface is built against.
            misfire_policy="fire_once",
        )
        return SkillResult(
            ok=True,
            value={"armed": True, "schedule_id": str(sid), "cadence": declared.schedule},
            actions_taken=[
                f"armed the digest on '{declared.schedule}' for "
                f"{len(declared.recipients)} recipient(s), declared at "
                f"{audience_path()}",
                NEEDS_A_HOST,
            ],
        )

    row = existing[0]
    changes: list[str] = []
    if row.state == "paused" and not dry_run:
        pulse_api.resume(pulse_api.ScheduleId(row.id))
        changes.append("resumed")
    declared_kind = cadence.kind
    if row.cadence_kind != declared_kind or row.name != f"Arrival digest @ {declared.schedule}":
        if not dry_run:
            pulse_api.reschedule(pulse_api.ScheduleId(row.id), cadence=cadence)
        changes.append(f"rescheduled to '{declared.schedule}'")

    return SkillResult(
        ok=True,
        value={
            "armed": True,
            "schedule_id": row.id,
            "cadence": declared.schedule,
            "changed": changes,
            "dry_run": dry_run,
        },
        actions_taken=[
            (
                f"the digest was already armed; {', '.join(changes)}"
                if changes
                else "the digest was already armed on the declared cadence; nothing changed"
            ),
            NEEDS_A_HOST,
        ],
    )


__all__ = ["ACTION", "NEEDS_A_HOST", "PULSE_HOST", "run"]
