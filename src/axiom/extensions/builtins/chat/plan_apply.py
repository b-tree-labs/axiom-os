# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Plan, then apply: a destructive step runs only against an approval of exactly what was planned.

A destructive command never runs from a single call that an agent (or a stray keystroke) can make. It is
split in two:

* ``propose(kind, subject)`` computes what *would* happen, hashes it (SHA-256 of the canonical JSON of the
  kind and the subject), and records a held action in the **existing approval queue**
  (``ApprovalGate`` / ``default_approval_path``, the file ``axi approve`` reads). It returns a ``Plan``.
  Nothing has changed yet.
* ``apply(plan_hash, approval_id, recompute=..., execute=...)`` runs the step only when all of these hold:

  1. the approval record exists, belongs to this kind of plan, and carries this hash;
  2. a person approved it (``ActionStatus.APPROVED`` with a decider); pending raises ``ApprovalRequired``
     naming how to approve, rejected raises ``ApprovalRejected``;
  3. the plan has not expired;
  4. it has not been applied before (a claimed record is marked running *before* the step executes, so a
     second call, or a crash and retry, is a replay and is refused);
  5. recomputing the subject *now* hashes to the same value (the world did not move since it was planned).

Generic on purpose: nothing here knows what ``execute`` does. Secrets are never put in a plan: callers
pass paths, ids and digests, and ``propose`` refuses a subject that looks like it holds a credential.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_TTL_SECONDS = 15 * 60
ACTION_PREFIX = "plan:"


class PlanRefused(Exception):
    """Base for every reason ``apply`` refuses. ``str(exc)`` is a sentence for the person."""


class ApprovalRequired(PlanRefused):
    """The plan exists and is waiting for a person. Carries the approval id and how to answer it."""

    def __init__(self, plan_hash: str, approval_id: str, how: str) -> None:
        self.plan_hash = plan_hash
        self.approval_id = approval_id
        super().__init__(f"plan {plan_hash[:12]} is waiting for approval {approval_id}. {how}")


class ApprovalRejected(PlanRefused):
    pass


class PlanMismatch(PlanRefused):
    pass


class PlanExpired(PlanRefused):
    pass


class PlanReplayed(PlanRefused):
    pass


@dataclass(frozen=True)
class Plan:
    kind: str
    subject: dict[str, Any]
    plan_hash: str
    approval_id: str
    summary: str
    created_at: str
    expires_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "plan_hash": self.plan_hash,
            "approval_id": self.approval_id,
            "summary": self.summary,
            "expires_at": self.expires_at,
            "subject": self.subject,
        }


def plan_hash(kind: str, subject: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of ``{kind, subject}``: key order and whitespace cannot change it."""
    canon = json.dumps({"kind": kind, "subject": subject}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _gate(store: Any = None):
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore, default_approval_path

    return ApprovalGate(store if store is not None else FileActionStore(default_approval_path()))


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def propose(
    kind: str,
    subject: dict[str, Any],
    *,
    summary: str,
    requested_by: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    store: Any = None,
    now: datetime | None = None,
) -> Plan:
    """Hash the plan and hold it in the approval queue. Changes nothing else."""
    from axiom.infra.orchestrator.actions import Action, ActionCategory

    if _looks_secret(json.dumps(subject, default=str)):
        raise ValueError("a plan subject must not carry a credential; pass ids and digests instead")
    created = _now(now)
    digest = plan_hash(kind, subject)
    expires = created + timedelta(seconds=ttl_seconds)
    action = Action(
        name=f"{ACTION_PREFIX}{kind}",
        params={
            "plan_hash": digest,
            "summary": summary,
            "subject": subject,
            "expires_at": expires.isoformat(),
            "requested_by": requested_by,
        },
        category=ActionCategory.WRITE,
        created_at=created.isoformat(),
        reason=f"destructive step planned: {summary}",
    )
    _gate(store).record(action)
    return Plan(kind, subject, digest, action.action_id, summary, created.isoformat(), expires.isoformat())


def approve_in_person(plan: Plan, decided_by: str, *, store: Any = None) -> None:
    """Record a person's approval of ``plan``. Only a surface that is a person's own terminal calls this;
    anything else leaves the plan pending until ``axi approve ok <id>``."""
    if not decided_by:
        raise ValueError("an approval must name who approved it")
    _gate(store).approve(plan.approval_id, decided_by=decided_by)


def apply(
    plan_hash_value: str,
    approval_id: str,
    *,
    kind: str,
    recompute: Callable[[], dict[str, Any]],
    execute: Callable[[dict[str, Any]], Any],
    store: Any = None,
    now: datetime | None = None,
) -> Any:
    """Run ``execute`` against an approved plan, or raise a ``PlanRefused`` that says why not."""
    from axiom.infra.orchestrator.actions import ActionStatus

    gate = _gate(store)
    action = gate.get(approval_id)
    if action is None:
        raise ApprovalRequired(plan_hash_value, approval_id, "No approval record with that id exists.")
    if action.name != f"{ACTION_PREFIX}{kind}":
        raise PlanMismatch(f"approval {approval_id} is for {action.name!r}, not a {kind} plan")
    if action.params.get("plan_hash") != plan_hash_value:
        raise PlanMismatch(
            f"approval {approval_id} was given for plan {str(action.params.get('plan_hash'))[:12]}, "
            f"not {plan_hash_value[:12]}"
        )
    if action.status == ActionStatus.PENDING:
        raise ApprovalRequired(
            plan_hash_value, approval_id, f"Approve it with: {_brand_cli()} approve ok {approval_id}"
        )
    if action.status == ActionStatus.REJECTED:
        raise ApprovalRejected(
            f"approval {approval_id} was refused" + (f": {action.error}" if action.error else "")
        )
    if action.status != ActionStatus.APPROVED:
        raise PlanReplayed(
            f"plan {plan_hash_value[:12]} was already applied ({action.status.value}); a plan runs once"
        )
    if not action.decided_by:
        raise ApprovalRequired(plan_hash_value, approval_id, "The approval names no approver, so it does not count.")
    try:
        expires = datetime.fromisoformat(str(action.params.get("expires_at")))
    except ValueError:
        raise PlanExpired("the plan carries no readable expiry, so it is treated as expired") from None
    if _now(now) > expires:
        raise PlanExpired(f"plan {plan_hash_value[:12]} expired at {expires.isoformat()}; plan again")
    current = recompute()
    if plan_hash(kind, current) != plan_hash_value:
        raise PlanMismatch(
            f"what the plan would do has changed since it was approved (plan {plan_hash_value[:12]}); "
            "nothing was changed. Plan again."
        )
    # Claim before executing: a second call, or a retry after a crash, finds the record no longer approved.
    action.status = ActionStatus.RUNNING
    gate.record(action)
    try:
        out = execute(current)
    except Exception as exc:
        action.status = ActionStatus.FAILED
        action.error = f"{type(exc).__name__}: {exc}"
        gate.record(action)
        raise
    action.status = ActionStatus.COMPLETED
    action.completed_at = _now(now).isoformat()
    gate.record(action)
    return out


def _looks_secret(text: str) -> bool:
    try:
        from axiom.infra.secret_scan import scan_text

        return bool(scan_text(text))
    except Exception:  # noqa: BLE001 - an unavailable scanner must not be a way around the check
        return False
