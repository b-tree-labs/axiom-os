# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Human-in-the-loop approval gate.

Classifies actions as read-only (auto-approve) or write (require human
confirmation). For safety-critical operations, all writes must be
explicitly approved.

Usage:
    gate = ApprovalGate()

    # Read-only → auto-approved
    action = create_action("query_docs")
    gate.submit(action)  # action.status == APPROVED

    # Write → pending human approval
    action = create_action("doc_publish", {"source": "docs/prds/prd_foo.md"})
    gate.submit(action)  # action.status == PENDING
    gate.pending()       # [action]
    gate.approve(action.action_id)  # action.status == APPROVED
"""

from __future__ import annotations

from axiom.infra.orchestrator.actions import (
    Action,
    ActionCategory,
    ActionStatus,
)


class ApprovalGate:
    """Manages the approval lifecycle for actions.

    Read-only actions are auto-approved. Write actions are held
    for human confirmation.
    """

    def __init__(self):
        self._actions: dict[str, Action] = {}

    def submit(self, action: Action) -> Action:
        """Submit an action for approval.

        Read-only actions are automatically approved.
        Write actions are placed in pending state.

        Returns:
            The action (possibly with updated status).
        """
        self._actions[action.action_id] = action

        if action.category == ActionCategory.READ:
            action.approve()

        return action

    def approve(self, action_id: str) -> Action | None:
        """Approve a pending action."""
        action = self._actions.get(action_id)
        if action and action.status == ActionStatus.PENDING:
            action.approve()
        return action

    def reject(self, action_id: str, reason: str = "") -> Action | None:
        """Reject a pending action."""
        action = self._actions.get(action_id)
        if action and action.status == ActionStatus.PENDING:
            action.reject(reason)
        return action

    def pending(self) -> list[Action]:
        """Return all actions awaiting approval."""
        return [
            a for a in self._actions.values()
            if a.status == ActionStatus.PENDING
        ]

    def get(self, action_id: str) -> Action | None:
        """Look up an action by ID."""
        return self._actions.get(action_id)

    def all_actions(self) -> list[Action]:
        """Return all tracked actions."""
        return list(self._actions.values())
