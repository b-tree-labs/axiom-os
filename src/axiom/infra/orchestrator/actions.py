# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Typed, serializable action intents for the orchestrator.

Actions represent intended operations that may require approval before
execution. They flow through the approval gate before being executed.

Status lifecycle: pending → approved → completed (or rejected)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class ActionStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    #: Claimed by a runner and in flight. Written *before* execution, so a
    #: process that dies mid-action leaves a record saying it was running
    #: rather than one still saying approved — the difference between "this
    #: may have half-happened" and "this never started".
    RUNNING = "running"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"


class ActionCategory(Enum):
    """Whether an action is read-only or a write that requires approval."""
    READ = "read"
    WRITE = "write"


@dataclass
class Action:
    """A typed, serializable intent.

    Examples:
        Action(name="doc.publish", params={"source": "docs/prds/prd_foo.md"})
        Action(name="sense.ingest", params={"source": "all"})
        Action(name="query_docs", params={}, category=ActionCategory.READ)
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    category: ActionCategory = ActionCategory.WRITE
    status: ActionStatus = ActionStatus.PENDING
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    #: Who answered, and when. An approval that cannot name its approver is not
    #: a safety record: "this was approved" is only worth anything alongside
    #: "by whom". Auto-approved reads record ``@gate:auto`` rather than being
    #: left blank, so an unattributed decision is always a bug and never a
    #: category of normal.
    decided_by: str | None = None
    decided_at: str | None = None

    #: Why a human was asked, when something other than the action's own
    #: category demanded it — a site rule, a GUARD consult, a hook. Distinct
    #: from ``error``, which carries the reason a human said *no*. A queue that
    #: shows what is waiting but not why is a queue that gets approved
    #: reflexively.
    reason: str | None = None

    def approve(self, decided_by: str = "@gate:auto") -> None:
        self.status = ActionStatus.APPROVED
        self.decided_by = decided_by
        self.decided_at = datetime.now(UTC).isoformat()

    def reject(self, reason: str = "", decided_by: str = "@gate:auto") -> None:
        self.status = ActionStatus.REJECTED
        self.error = reason
        self.decided_by = decided_by
        self.decided_at = datetime.now(UTC).isoformat()

    def complete(self, result: dict[str, Any] | None = None) -> None:
        self.status = ActionStatus.COMPLETED
        self.completed_at = datetime.now(UTC).isoformat()
        self.result = result

    def fail(self, error: str) -> None:
        self.status = ActionStatus.FAILED
        self.completed_at = datetime.now(UTC).isoformat()
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "name": self.name,
            "params": self.params,
            "category": self.category.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Action:
        return cls(
            action_id=d.get("action_id", ""),
            name=d["name"],
            params=d.get("params", {}),
            category=ActionCategory(d.get("category", "write")),
            status=ActionStatus(d.get("status", "pending")),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at"),
            result=d.get("result"),
            error=d.get("error"),
            decided_by=d.get("decided_by"),
            decided_at=d.get("decided_at"),
            reason=d.get("reason"),
        )


# Pre-defined action names and their categories
ACTION_REGISTRY: dict[str, ActionCategory] = {
    # Read-only actions (auto-approved)
    "query_signals": ActionCategory.READ,
    "query_docs": ActionCategory.READ,
    "sense_status": ActionCategory.READ,
    "list_providers": ActionCategory.READ,
    "read_draft": ActionCategory.READ,
    "doc_check_links": ActionCategory.READ,
    "doc_diff": ActionCategory.READ,
    "read_file": ActionCategory.READ,
    "list_files": ActionCategory.READ,
    "search_docs": ActionCategory.READ,
    "signal_status": ActionCategory.READ,
    "discover_verbs": ActionCategory.READ,

    # File write (requires approval)
    "write_file": ActionCategory.WRITE,

    # Review tools (read = inspect, write = decide/complete)
    "review_start": ActionCategory.READ,
    "review_get_item": ActionCategory.READ,
    "review_progress": ActionCategory.READ,
    "review_decide": ActionCategory.WRITE,
    "review_complete": ActionCategory.WRITE,

    # Email tools (read = list/preview, write = draft/send)
    "email_list": ActionCategory.READ,
    "email_preview": ActionCategory.READ,
    "email_draft": ActionCategory.WRITE,
    "email_send": ActionCategory.WRITE,

    # Write actions (require approval)
    "sense_ingest": ActionCategory.WRITE,
    "sense_draft": ActionCategory.WRITE,
    "doc_generate": ActionCategory.WRITE,
    "doc_publish": ActionCategory.WRITE,
    "write_inbox_note": ActionCategory.WRITE,
}


def create_action(name: str, params: dict[str, Any] | None = None) -> Action:
    """Create an action with the correct category from the registry."""
    category = ACTION_REGISTRY.get(name, ActionCategory.WRITE)
    return Action(name=name, params=params or {}, category=category)
