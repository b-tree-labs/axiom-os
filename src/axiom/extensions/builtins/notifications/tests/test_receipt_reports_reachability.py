# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A receipt that says "succeeded" has to mean somebody can read the alert.

With no database and no channel configured — the exact situation of an adopter
running a monitor from their own laptop — `send()` routes to the in-memory inbox
and reports `outcome="succeeded"` with a receipt id. The rows it wrote are
discarded when the process exits, so the monitor prints "1 alert(s) sent" and
nobody is ever told anything.

The verdict stays "succeeded" (delivery to the selected channel did happen), but
the receipt now also says WHERE it landed, so a caller whose whole purpose is to
reach a person can tell "the operator has it" from "it went into a dict that no
longer exists".
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.send import (
    NotificationPayload,
    Priority,
    SendContext,
    send,
)
from axiom.governance.classification import Classification


def _send(ctx):
    return send(
        ctx,
        actor="@rod-sentinel:netl",
        recipient="@sam:netl",
        payload=NotificationPayload(summary="rod noise elevated"),
        classification=Classification("internal"),
        priority=Priority.HIGH,
    )


def test_in_memory_inbox_is_reported_as_not_durable():
    """The laptop case: it 'succeeded' into a store that dies with the process."""
    # `default()` is the context the shipped monitor spine builds.
    receipt = _send(SendContext.default())

    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"
    assert receipt.durable is False


def test_durable_inbox_is_reported_as_durable():
    """Negative control: the flag tracks the store, it is not always False."""
    import sqlalchemy
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.notifications.db_models import Base
    from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

    engine = sqlalchemy.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    ctx = SendContext(inbox_store=DatabaseInboxStore(engine=engine))
    # The inbox row's FK needs its receipt to exist; the store writes both.
    ctx.inbox_store.write_alert(recipient="@sam:netl", summary="seed")

    assert ctx.inbox_store.durable is True


def test_stores_declare_their_own_durability():
    """The adapter must not have to guess by class name."""
    from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

    assert InMemoryInboxStore().durable is False
    assert DatabaseInboxStore.durable is True
