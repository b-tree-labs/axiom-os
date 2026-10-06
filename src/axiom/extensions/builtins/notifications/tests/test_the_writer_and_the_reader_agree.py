# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The two halves of the inbox must be pointed at the same place.

Found while walking stage 0 of the oversight journey, looking for why an
armed digest would not reach anybody.

``notifications.list_inbox`` — the READER — resolves its store through
``default_inbox_store()``: Postgres when reachable, in-memory as a stated
fallback. ``SendContext`` — the WRITER — defaulted to ``InMemoryInboxStore``
unconditionally. So the reader queried a durable table while every send
disappeared into the writing process's memory, and both halves reported
success.

That is worse than "the inbox is not durable". It is one feature whose two
ends disagree about where its data lives, and neither end can tell.

The reason the writer could not simply be pointed at the durable store is a
real constraint rather than an oversight: ``notifications_inbox.receipt_id``
is a NOT NULL foreign key to ``delivery_receipts``, and nothing on the
``send()`` path wrote a receipt row — the receipts lived in
``SendContext.receipts``, a dict. Writing the inbox row first fails:

    IntegrityError: FOREIGN KEY constraint failed

``write_alert`` had already solved this for the alert path by writing its
receipt and its inbox row in one transaction. These tests hold ``write`` to
the same bargain, and hold ``send()`` to finishing the receipt it started.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.notifications.db_models import (
    Base,
    DeliveryReceipt,
    NotificationsInbox,
)
from axiom.extensions.builtins.notifications.inbox import InboxQuery
from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore
from axiom.extensions.builtins.notifications.send import (
    NotificationPayload,
    SendContext,
    send,
)
from axiom.governance.classification import Classification


@pytest.fixture
def durable():
    """A real durable store with the foreign key ACTUALLY ENFORCED.

    SQLite does not enforce foreign keys unless asked, so a fixture without
    the pragma writes an inbox row pointing at a receipt that does not exist
    and calls it a pass. Postgres — every real deployment — rejects it. A
    test suite that cannot fail on the constraint it is testing is not
    testing the constraint, so the pragma is part of the fixture rather
    than an optional nicety.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _enforce_fks(dbapi_connection, _record):  # pragma: no cover — driver hook
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return DatabaseInboxStore(engine=engine)


def _ctx(store) -> SendContext:
    from axiom.extensions.builtins.notifications.channels.inbox import (
        InboxChannelAdapterProvider,
    )

    ctx = SendContext(inbox_store=store)
    ctx.registry.register(InboxChannelAdapterProvider(store=store))
    return ctx


def _send(ctx, *, summary="2 cases waiting on you", recipient="@robin", dedup_key=None):
    return send(
        ctx,
        actor="@receipts:local",
        recipient=recipient,
        payload=NotificationPayload(summary=summary, body="node-a stopped reporting"),
        classification=Classification.INTERNAL,
        intent="receipts.digest",
        dedup_key=dedup_key,
    )


def test_a_send_lands_where_the_reader_looks(durable):
    """The split-brain test. What `send` writes, `list_inbox`'s store reads."""
    receipt = _send(_ctx(durable))
    assert receipt.outcome == "succeeded"
    rows = durable.query(InboxQuery(recipient="@robin"))
    assert [r.summary for r in rows] == ["2 cases waiting on you"]


def test_the_inbox_row_has_the_receipt_its_foreign_key_promises(durable):
    """`receipt_id` is NOT NULL and points at `delivery_receipts`. A row
    written without one is not merely undocumented, it is rejected."""
    _send(_ctx(durable))
    with Session(durable._engine) as s:
        row = s.query(NotificationsInbox).one()
        assert s.get(DeliveryReceipt, row.receipt_id) is not None, (
            "the inbox row points at a receipt that was never written"
        )


def test_a_durable_send_says_it_is_durable(durable):
    """`durable` answers "did this outlive the process". Against a real
    store it must, or the flag is noise."""
    assert _send(_ctx(durable)).durable is True


def test_the_persisted_receipt_carries_the_outcome_not_pending_for_ever(durable):
    """The receipt row is opened before dispatch — it has to be, for the
    foreign key — so something must go back and finish it. A receipt frozen
    at `pending` is a record that says nothing happened."""
    sent = _send(_ctx(durable))
    with Session(durable._engine) as s:
        stored = s.get(DeliveryReceipt, sent.id)
        assert stored is not None
        assert stored.outcome == "succeeded", (
            f"the stored receipt says {stored.outcome!r} while the returned one "
            f"says {sent.outcome!r} — the two must not disagree"
        )
        assert stored.channel_selected == "inbox"
        assert stored.latency_ms is not None


def test_the_stored_receipt_does_not_carry_the_message_body(durable):
    """A receipt is delivery metadata. The body is somebody's operational
    detail and belongs in the inbox row a reader has to be entitled to,
    not duplicated into an audit table with different retention."""
    sent = _send(_ctx(durable))
    with Session(durable._engine) as s:
        stored = s.get(DeliveryReceipt, sent.id)
        blob = str(stored.envelope_json)
        assert "node-a stopped reporting" not in blob, blob


def test_with_no_durable_store_the_send_still_works_and_says_it_is_not_durable():
    """The in-memory fallback is the right answer on a dev box with no
    database. It must stay a fallback rather than becoming a failure — and it
    must keep saying it did not outlive the process."""
    from axiom.extensions.builtins.notifications.channels.inbox import (
        InboxChannelAdapterProvider,
    )
    from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore

    store = InMemoryInboxStore()
    ctx = SendContext(inbox_store=store)
    ctx.registry.register(InboxChannelAdapterProvider(store=store))
    receipt = _send(ctx)
    assert receipt.outcome == "succeeded"
    assert receipt.durable is False


def test_the_bare_constructor_does_not_reach_for_a_database():
    """A dataclass default that resolved a durable store would have every
    test in the tree writing rows into the developer's real Postgres. The
    resolution belongs in `default()`, which production paths use."""
    from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore

    assert isinstance(SendContext().inbox_store, InMemoryInboxStore)


def test_the_writer_defaults_to_the_same_seam_the_reader_uses(monkeypatch):
    """The actual defect: `SendContext` must resolve its store through the
    shared resolver, not construct an in-memory one unconditionally."""
    from axiom.extensions.builtins.notifications import inbox_db

    sentinel = object()
    monkeypatch.setattr(inbox_db, "_DEFAULT_STORE", sentinel)
    assert SendContext.default(rehydrate=False).inbox_store is sentinel, (
        "SendContext built its own store instead of using default_inbox_store()"
    )


def test_a_repeat_with_the_same_dedup_key_writes_no_second_inbox_row(durable):
    """Suppression has to happen before the row, or the dedup log protects
    somebody's attention while the inbox fills up behind it.

    Note what `send` returns here: the PRIOR receipt, verbatim. Only a hit
    from a different process synthesizes a `suppressed_duplicate` outcome,
    because that run's receipt is not in this run's memory to return.
    """
    ctx = _ctx(durable)
    first = _send(ctx, dedup_key="d")
    second = _send(ctx, dedup_key="d")
    assert second.id == first.id
    assert len(durable.query(InboxQuery(recipient="@robin"))) == 1
