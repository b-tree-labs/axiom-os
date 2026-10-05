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
from axiom.infra.orchestrator.approval_store import (
    ActionStore,
    FileActionStore,
    InMemoryActionStore,
)


class ApprovalGate:
    """Manages the approval lifecycle for actions.

    Read-only actions are auto-approved. Write actions are held
    for human confirmation.

    **The returned action is the authoritative one.** Read the status off what
    a method returns, never off the object you passed in earlier.

    This used to be a distinction without a difference: the gate kept actions in
    a dictionary, so it handed back the very object the caller submitted and
    mutating it in place updated both. A durable store cannot do that. It
    serializes on the way in and reconstructs on the way out, so ``approve()``
    resolves a *copy*, and the caller's original reference stays PENDING forever
    while the record says APPROVED.

    Trusting the stale reference is the dangerous direction here: it reads as
    "not approved yet", which looks like a working gate right up until someone
    wonders why an approved action never ran. Two unit tests asserted on the
    passed-in object and caught this immediately, which is the argument for
    having written them against the object rather than the return value.
    """

    def __init__(self, store: ActionStore | None = None, *, durable: bool = False):
        """
        Args:
            store: Where actions live between submission and resolution.
            durable: Use the file-backed store, so a pending action survives the
                process that proposed it. Ignored when ``store`` is given.

        The default stays in-memory, deliberately, and the reasoning is worth
        keeping because durable-by-default was tried first and was wrong twice.

        It wrote to the caller's real ``~/.axi/state`` the moment any test
        constructed a gate, which is not a default anyone opts into knowingly.

        And it would half-persist the one production caller: ``cli/plan.py``
        pairs this gate with an in-memory plan store and says so — "persistence
        is in-memory until #76". A durable approval queue beside an ephemeral
        plan store is worse than either, because an approval would outlive the
        plan it approves.

        Durability is for the caller that pauses: an agent that asks permission
        and then forgets it asked has not asked. Those callers opt in, and know
        they are opting in.
        """
        if store is not None:
            self._store: ActionStore = store
        elif durable:
            self._store = FileActionStore()
        else:
            self._store = InMemoryActionStore()

    def submit(self, action: Action) -> Action:
        """Submit an action for approval.

        Read-only actions are automatically approved.
        Write actions are placed in pending state.

        Returns:
            The action, with its status settled. Use this reference from here
            on; see the class docstring on why the one you passed in may not
            track it.
        """
        if action.category == ActionCategory.READ:
            action.approve()

        # Recorded after the auto-approve so a read action is never briefly
        # persisted as pending, and recorded before returning so a caller that
        # acts on the return value cannot outrun the record.
        self._store.put(action)
        return action

    def approve(self, action_id: str, *, decided_by: str = "@gate:auto") -> Action | None:
        """Approve a pending action, recording who approved it.

        An action that has expired is no longer pending, so this returns it
        unchanged rather than approving it. Approving something whose window
        closed would be approving against a world that has moved.
        """
        action = self._store.get(action_id)
        if action and action.status == ActionStatus.PENDING:
            action.approve(decided_by)
            self._store.put(action)
        return action

    def reject(
        self, action_id: str, reason: str = "", *, decided_by: str = "@gate:auto"
    ) -> Action | None:
        """Reject a pending action, recording who rejected it."""
        action = self._store.get(action_id)
        if action and action.status == ActionStatus.PENDING:
            action.reject(reason, decided_by)
            self._store.put(action)
        return action

    def record(self, action: Action) -> None:
        """Persist an action's current state.

        For a caller that owns the transition itself — a runner claiming work,
        or completing it. ``approve``/``reject`` are the gate's own vocabulary
        and stay narrow; everything else says what it did and writes it down.
        """
        self._store.put(action)

    def pending(self) -> list[Action]:
        """Return all actions awaiting approval."""
        return [a for a in self._store.all() if a.status == ActionStatus.PENDING]

    def get(self, action_id: str) -> Action | None:
        """Look up an action by ID."""
        return self._store.get(action_id)

    def all_actions(self) -> list[Action]:
        """Return all tracked actions."""
        return list(self._store.all())
