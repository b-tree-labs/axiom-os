# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signed approvals: the gate's decisions as records in the ``approvals`` log (logbook unit X3).

With the flag ``approvals_signed`` on, a held action is approved or rejected only by a person
signing an ``ACTION_DECISION`` whose subject is the action. This module fills the seam
``axiom.infra.orchestrator.decisions`` defines (``infra`` never imports this extension), and
installs two steps on the approvals log's signatures:

- before the signature, under the draft lock: the action still exists, is still pending, and its
  name and parameters are the ones the card showed; otherwise the person presents again;
- after the commit: the gate moves the action (``ApprovalGate.decide_attested``), idempotent on
  the attestation id. If that step is lost (the process stops between the two), :func:`reconcile`
  finds the record and moves the action; ``approval.pending`` and the pending route run it.

A rule decision (an auto-approved read, ``rule:read_only``) creates no record here.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from axiom.infra.orchestrator.actions import Action, ActionStatus
from axiom.infra.orchestrator.approval import ApprovalGate, DecisionRefused
from axiom.infra.skills import SkillResult

from . import service
from .service import APPROVALS, DECISION_TYPE, AttestRefused

log = logging.getLogger(__name__)

LOGBOOK_FILE = Path(__file__).resolve().parent / "logbooks" / "approvals.toml"
FLAG = "approvals_signed"
#: How many pending actions one reconcile looks at (Axiom #1780: no unbounded work per call).
RECONCILE_LIMIT = 200

_gate_factory: Any = None
_current: ContextVar[ApprovalGate | None] = ContextVar("axiom_attest_approvals_gate", default=None)


def flag_on() -> bool:
    """Whether signed approvals are on (the ``approvals_signed`` flag, ADR-210)."""
    from axiom.extensions.builtins.flags.service import env_override

    pinned = env_override(FLAG)
    if pinned is not None:
        return pinned == "on"
    try:
        from axiom.extensions.builtins.flags.service import FlagService

        return FlagService().is_enabled(FLAG, node=os.environ.get("AXIOM_NODE") or None)
    except Exception:  # noqa: BLE001 - an unreadable flag store leaves the default (off)
        return False


def _default_gate() -> ApprovalGate:
    from axiom.infra.orchestrator.approval_store import FileActionStore
    from axiom.infra.paths import get_user_state_dir

    return ApprovalGate(FileActionStore(get_user_state_dir() / "orchestrator" / "approvals.json"))


def gate() -> ApprovalGate:
    """The gate a decision moves: the caller's, when a decision verb passed one, else the node's."""
    current = _current.get()
    if current is not None:
        return current
    return (_gate_factory or _default_gate)()


def _decision_fields(draft_or_record: dict[str, Any]) -> dict[str, Any]:
    return ((draft_or_record.get("content") or {}).get("fields")) or {}


def _before_sign(draft: dict[str, Any]) -> None:
    if draft.get("entry_type") != DECISION_TYPE:
        return
    fields = _decision_fields(draft)
    action = gate().get(str(fields.get("action_id") or ""))
    if action is None:
        raise AttestRefused(f"no held action {fields.get('action_id')!r}; nothing to decide")
    if action.status != ActionStatus.PENDING:
        raise AttestRefused(f"action {action.action_id} is already {action.status.value}")
    if (action.name, service.decision_summary(action.params)) != (
        fields.get("action_name"),
        fields.get("summary"),
    ):
        raise AttestRefused(
            f"action {action.action_id} changed since the decision was presented; present it again"
        )


def _after_sign(record: dict[str, Any]) -> None:
    if record.get("entry_type") != DECISION_TYPE:
        return
    gate().decide_attested(str(_decision_fields(record).get("action_id") or ""), attestation=record)


def _decision_for(action_id: str) -> dict[str, Any] | None:
    """The first signed decision whose subject is ``action_id``, or None."""
    import json

    from sqlalchemy import text

    from . import store

    with store.session_scope() as s:
        row = s.execute(
            text(
                "SELECT record FROM attest_records WHERE logbook = :logbook "
                "AND entry_type = :type AND record -> 'subjects' @> CAST(:subject AS jsonb) "
                "ORDER BY recorded_at, seq LIMIT 1"
            ),
            {
                "logbook": APPROVALS,
                "type": DECISION_TYPE,
                "subject": json.dumps([{"kind": "action", "value": action_id}]),
            },
        ).first()
    return dict(row[0]) if row else None


def reconcile(g: ApprovalGate | None = None, *, limit: int = RECONCILE_LIMIT) -> list[Action]:
    """Move every pending action whose signed decision exists; returns the actions moved.

    For a lost after-sign step. Idempotent on the attestation id; a record that does not decide
    its action (another log, not a person) is reported and changes nothing."""
    g = g or gate()
    moved = []
    for action in g.pending()[:limit]:
        rec = _decision_for(action.action_id)
        if rec is None:
            continue
        try:
            moved.append(g.decide_attested(action.action_id, attestation=rec))
        except DecisionRefused as exc:
            log.warning("approvals reconcile left %s pending: %s", action.action_id, exc)
    return moved


#: Where a person rejects a held action at once, without the confirm card (held card D1): a gate
#: route, so only a person's own sign-in reaches it (``webgate/api/approval_reject_route.py``).
REJECT_PATH = "/gate/approvals/{action_id}/reject"


def reject_address(action_id: str) -> str:
    """The gate address that rejects ``action_id`` in one step (the pending list names it)."""
    return REJECT_PATH.format(action_id=action_id)


class AttestDecisionSigner:
    """The attest side of ``axiom.infra.orchestrator.decisions``."""

    def active(self) -> bool:
        return flag_on()

    def card_address(self, action_id: str) -> str:
        return f"/#/control?decide={action_id}"

    def record(self, attestation_id: str) -> dict[str, Any] | None:
        return service.record(attestation_id)

    def decide(self, g: ApprovalGate, action: Action, *, approving: bool, reason: str, ctx: Any) -> SkillResult:
        from .skills._common import fail, resolve_site

        surface = getattr(ctx, "surface", None) or "cli"
        handle = str(getattr(getattr(ctx, "principal", None), "handle", "") or "")
        actor = str(getattr(ctx, "actor", "") or handle)
        if surface != "cli":
            return fail(f"a decision is signed by a person at the prompt or on the card, not over {surface}")
        if not handle.startswith("@") or not actor.startswith("@"):
            return fail("a decision is signed by a person; nothing was decided")
        if getattr(ctx, "user_prompt", None) is None:
            return fail(
                "a decision needs a person at the prompt; nothing was decided. Sign it on the card: "
                + self.card_address(action.action_id)
            )
        site, err = resolve_site({})
        if err:
            return fail(err)
        try:
            draft_id = service.create_decision_draft(
                site_id=site, action=action, approving=approving, reason=reason,
                origin="human", for_principal=handle,
            )  # fmt: skip
            rules = service.rules_for(draft_id)
        except AttestRefused as exc:
            return fail(f"not decided: {exc}")
        if rules.fresh_within_seconds is not None or rules.presence is not None or "personal" not in rules.sign_devices:
            return fail(
                f"{rules.what} is signed with a fresh sign-in; nothing was decided here. "
                f"Sign it on the card: {self.card_address(action.action_id)}"
            )
        from .skills.sign import _respond

        token = _current.set(g)
        try:
            return _respond(draft_id, site, ctx)
        finally:
            _current.reset(token)


def install(*, gate_factory: Any = None) -> None:
    """Fill the decision seam and the approvals log's sign steps. Idempotent."""
    global _gate_factory
    from axiom.infra.orchestrator import decisions

    if gate_factory is not None:
        _gate_factory = gate_factory
    decisions.set_signer(AttestDecisionSigner())
    service.set_sign_hooks(APPROVALS, before=_before_sign, after=_after_sign)


def uninstall() -> None:
    global _gate_factory
    from axiom.infra.orchestrator import decisions

    _gate_factory = None
    decisions.reset_signer()
    service.clear_sign_hooks(APPROVALS)


__all__ = ["FLAG", "LOGBOOK_FILE", "REJECT_PATH", "AttestDecisionSigner", "flag_on", "gate", "install", "reconcile", "reject_address", "uninstall"]
