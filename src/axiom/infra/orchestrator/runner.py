# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What happens after the human says yes.

The gate could hold an action and a human could answer it, and then nothing
consumed the answer. An approved action sat in the queue looking settled while
the work it authorised never ran, which is a worse failure than never asking:
the record says approved, the operator believes it happened, and it did not.

This is the other end. :class:`ActionRunner` picks up approved actions and
executes them through :func:`axiom.infra.skill_dispatch.invoke_capability` —
the same chokepoint the CLI and MCP go through, because an action approved as
``doc.publish`` must run the ``doc.publish`` everyone else means, under the same
site rules, writing the same ledger.

Reaching for ``SkillRegistry.invoke`` directly would skip the ``tool.pre_invoke``
hooks, the GUARD consult and the refusal ledger. A guard test in
``tests/infra/test_skill_dispatch.py`` enforces that, and it caught this module
doing exactly that on the first attempt.

Executing an approved action is a surface crossing, not composition inside one
action: the boundary it crosses is the human decision. So it is routed and it
carries its own surface name, ``runner``.

At-least-once, and honest about it
----------------------------------

The runner claims an action by writing ``RUNNING`` before executing, so a crash
mid-flight leaves a record that says so rather than one that says approved.
On restart a stranded ``RUNNING`` action is **not** re-run automatically unless
its skill declares ``idempotent``. Re-running a non-idempotent write because a
process died is how one approval becomes two writes, and the operator is better
served by being told an action is stranded than by having it silently repeated.

``resume_stranded`` is the deliberate, human-triggered version of that decision.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from axiom.infra.orchestrator.actions import Action, ActionStatus
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.skill_dispatch import RUNNER_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry

log = logging.getLogger(__name__)

__all__ = ["ActionRunner", "RunReport"]


@dataclass
class RunReport:
    """What one pass did. Empty is the normal, healthy shape."""

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unroutable: list[str] = field(default_factory=list)
    stranded: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.completed or self.failed or self.unroutable or self.stranded)


class ActionRunner:
    """Executes approved actions, once, through the skill registry."""

    def __init__(
        self,
        gate: ApprovalGate,
        registry: SkillRegistry,
        ctx: SkillContext,
    ) -> None:
        self._gate = gate
        self._registry = registry
        self._ctx = ctx

    def _approved(self) -> Iterable[Action]:
        return [a for a in self._gate.all_actions() if a.status == ActionStatus.APPROVED]

    def _claim(self, action: Action) -> bool:
        """Mark RUNNING before doing anything, and refuse a lost race.

        Two runners on one queue is not exotic — it is a service plus somebody
        debugging. The claim is a compare-and-set through the store: re-read,
        confirm it is still APPROVED, then write RUNNING.
        """
        fresh = self._gate.get(action.action_id)
        if fresh is None or fresh.status != ActionStatus.APPROVED:
            return False
        fresh.status = ActionStatus.RUNNING
        self._gate.record(fresh)
        return True

    def run_approved(self) -> RunReport:
        """One pass. Returns what changed."""
        report = RunReport()

        for action in self._approved():
            if not self._registry.has(action.name):
                # Not a failure of the action; a failure to route it. Leaving it
                # APPROVED means a later deploy that registers the skill picks
                # it up, which beats burning the human's decision on a typo in
                # our own wiring.
                report.unroutable.append(action.action_id)
                log.warning(
                    "approved action %s names %r, which no skill provides; "
                    "leaving it approved rather than failing the human's decision",
                    action.action_id,
                    action.name,
                )
                continue

            if not self._claim(action):
                continue

            # invoke_capability already turns an exception into ok=False, so
            # there is no bare except here. A runner that added one would be
            # catching what the chokepoint has already handled and hiding the
            # traceback it put in errors.
            result = invoke_capability(
                self._registry,
                action.name,
                dict(action.params),
                self._ctx,
                surface=RUNNER_SURFACE,
                # Never re-queue what we are already re-running. A hook that
                # still demands approval for an action a human approved would
                # otherwise mint a fresh pending action on every pass, forever.
                hold_on_approval=False,
            )

            if result.ok:
                action.complete({"value": result.value})
                report.completed.append(action.action_id)
            else:
                action.fail("; ".join(result.errors) or "skill reported failure")
                report.failed.append(action.action_id)
            self._gate.record(action)

        return report

    def stranded(self) -> list[Action]:
        """Actions a previous process claimed and never finished."""
        return [a for a in self._gate.all_actions() if a.status == ActionStatus.RUNNING]

    def resume_stranded(self, *, force: bool = False) -> RunReport:
        """Deal with actions orphaned by a crash.

        Only re-runs what declares itself idempotent, unless ``force``. A
        non-idempotent write re-run because a process died is how one approval
        becomes two writes, and that is a decision for a person.
        """
        report = RunReport()
        for action in self.stranded():
            spec = self._registry.spec(action.name)
            if not force and not (spec and spec.idempotent):
                report.stranded.append(action.action_id)
                continue
            action.status = ActionStatus.APPROVED
            self._gate.record(action)
        if report.stranded:
            log.warning(
                "%d stranded action(s) left alone: not declared idempotent. "
                "Re-run deliberately with force=True after checking what landed.",
                len(report.stranded),
            )
        merged = self.run_approved()
        merged.stranded = report.stranded
        return merged
