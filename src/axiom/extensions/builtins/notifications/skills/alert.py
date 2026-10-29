# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`notifications alert` — post a durable alert to a principal's inbox.

The path a MONITOR uses. `notifications send` runs the full channel-routing
pipeline and keeps its receipts in memory, which means an alert it delivers is
invisible to any other process. A monitor's whole purpose is to tell somebody
who is somewhere else, so it needs the durable floor: a delivery receipt and an
inbox row, written together, that a chat in another process can read.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    recipient = (params.get("recipient") or "").strip()
    summary = (params.get("summary") or "").strip()
    if not recipient:
        return SkillResult(ok=False, errors=["missing required param: recipient"])
    if not summary:
        return SkillResult(ok=False, errors=["missing required param: summary"])

    from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

    try:
        written = DatabaseInboxStore().write_alert(
            recipient=recipient,
            summary=summary,
            priority=params.get("priority") or "normal",
            classification=params.get("classification") or "internal",
            actor=params.get("actor") or "@monitor:local",
            link=params.get("link") or "",
            dedup_key=(params.get("dedup_key") or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001
        # Say what to do about it. A monitor that cannot reach the inbox is a
        # monitor nobody hears, so this must not fail quietly.
        return SkillResult(
            ok=False,
            errors=[
                f"could not write the alert: {type(exc).__name__}: {exc}. "
                "The inbox is a database table — check the database is "
                "reachable and the notifications schema is provisioned."
            ],
        )

    return SkillResult(
        ok=True,
        value={
            "id": written.row_id,
            # The monitor recipe calls this the delivery proof, so it has to be
            # in what the monitor gets back — not only in the table.
            "receipt_id": written.receipt_id,
            "recipient": recipient,
            "summary": summary,
            "priority": params.get("priority") or "normal",
            "link": params.get("link") or "",
            "durable": True,
            # "Nothing was written" and "the same alert already stands" both end
            # with one inbox row; only this tells the author which happened.
            "deduplicated": written.deduplicated,
        },
    )
