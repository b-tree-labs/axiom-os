# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The incident stage's two scheduled capabilities (ADR-249 D2, D6; ADR-252).

- ``notifications.incident_tick`` runs every minute: it releases incidents
  whose hold has run out (heal first, then send ``urgent`` / ``high``, keep
  ``normal`` / ``low`` for the digest) and resolves incidents that have been
  quiet long enough.
- ``notifications.digest`` runs once a day: one message listing what agents
  fixed, what is held for the digest, what is still open after a person was
  sent it, and what was resolved. It is sent on a quiet day too, so silence
  means the digest ran and found nothing, not that it stopped running.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_DELIVERED = ("succeeded", "suppressed_duplicate")


def _stage():
    from axiom.extensions.builtins.notifications.incidents import default_stage

    return default_stage()


def incident_tick(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    report = _stage().tick()
    return SkillResult(
        ok=True,
        value=report,
        actions_taken=[", ".join(f"{k}={v}" for k, v in report.items())],
    )


def digest(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        hours = float(params.get("window_hours") or 24)
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=[f"window_hours must be a number, got {params.get('window_hours')!r}"])
    stage = _stage()
    d = stage.digest(window=timedelta(hours=hours))
    text = stage.digest_text(d)
    day = d.until.date().isoformat()
    counts = {
        "healed": len(d.healed),
        "open_quiet": len(d.open_quiet),
        "open_told": len(d.open_told),
        "resolved": len(d.resolved),
    }

    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        send,
    )
    from axiom.extensions.builtins.notifications.skills.send import _ctx
    from axiom.governance import Classification

    subject = f"Digest {day}: " + (
        "quiet" if d.quiet else ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items() if v)
    )
    receipt = send(
        _ctx(),
        actor="@herald",
        recipient=str(params.get("recipient") or "@operator"),
        payload=NotificationPayload(summary=subject, body=text),
        classification=Classification.INTERNAL,
        priority=Priority.LOW,
        intent="notification.digest",
        # One per day, however often the cadence or a person runs it.
        dedup_key=f"notifications.digest:{day}",
    )
    return SkillResult(
        ok=receipt.outcome in _DELIVERED,
        value={"day": day, "quiet": d.quiet, **counts, "receipt_id": receipt.id, "outcome": receipt.outcome},
        errors=[] if receipt.outcome in _DELIVERED else [f"digest not delivered: {receipt.outcome}"],
        actions_taken=[f"digest {day} -> {receipt.outcome}"],
    )


__all__ = ["digest", "incident_tick"]
