# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`notifications.list` must read the inbox alerts actually land in.

Found by a live cross-surface test, not by a unit test:

    $ axi notifications list --recipient @laptop:ben --unread
    (no inbox rows for @laptop:ben)

    chat, same principal, same moment: 3 unread alerts

Two stores. Chat's watcher read `DatabaseInboxStore`; the `list` verb read
`SendContext.inbox_store`, which is in-memory. One principal asking one
question of one system got two answers depending on which surface asked —
the precise failure "one chat, many surfaces" exists to prevent, and it was
invisible to every existing test because each store's own suite was green.

The verb is agent-reachable, so this was not only an operator inconvenience:
an agent asked "what alerts are outstanding?" was answered "none" while three
were.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.notifications.db_models import Base
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.inbox_db import (
    DatabaseInboxStore,
    set_default_inbox_store,
)
from axiom.extensions.builtins.notifications.skills import list_inbox

RECIPIENT = "@operator:reactor"


@pytest.fixture
def durable():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    store = DatabaseInboxStore(engine=engine)
    set_default_inbox_store(store)
    yield store
    set_default_inbox_store(None)


def _alert(store, summary, priority="urgent"):
    return store.write_alert(
        recipient=RECIPIENT,
        summary=summary,
        priority=priority,
        actor=f"@rod-sentinel:netl-{uuid.uuid4().hex[:4]}",
    )


class TestOneQuestionOneAnswer:
    def test_the_verb_sees_an_alert_written_to_the_durable_store(self, durable):
        _alert(durable, "Rod A reactivity insertion 0.18 $/s exceeds the 0.15 limit")

        result = list_inbox.run({"recipient": RECIPIENT, "unread_only": True}, None)

        assert result.ok
        summaries = [row["summary"] for row in result.value["items"]]
        assert summaries == [
            "Rod A reactivity insertion 0.18 $/s exceeds the 0.15 limit"
        ]

    def test_an_empty_in_memory_store_does_not_mask_a_durable_alert(self, durable):
        """The live failure exactly: the in-memory store is empty and says so,
        the database holds the alert. The verb must not answer from the empty
        one."""
        _alert(durable, "Primary coolant dT 8.2C against a 6.0C design band")

        # Whatever the send pipeline's own store holds, the read is durable.
        assert InMemoryInboxStore().all() == []

        result = list_inbox.run({"recipient": RECIPIENT, "unread_only": True}, None)

        assert result.ok
        assert len(result.value["items"]) == 1, (
            "the verb answered from an empty in-memory store while the durable "
            "inbox held an alert — the divergence this test exists for"
        )

    def test_the_verb_and_chat_agree_row_for_row(self, durable):
        """Not 'the verb returns something' — the two surfaces return the SAME
        rows. A count-only assertion would pass with the stores disagreeing
        about content."""
        for summary, priority in [
            ("Rod A reactivity insertion 0.18 $/s exceeds the 0.15 limit", "urgent"),
            ("Primary coolant dT 8.2C against a 6.0C design band", "high"),
            ("Pool level 12mm below nominal, makeup 1.2 L/min", "normal"),
        ]:
            _alert(durable, summary, priority)

        from axiom.extensions.builtins.notifications.inbox import list_unread

        chat_rows = list_unread(durable, recipient=RECIPIENT)
        verb = list_inbox.run({"recipient": RECIPIENT, "unread_only": True}, None)

        assert verb.ok
        assert [r["summary"] for r in verb.value["items"]] == [
            r.summary for r in chat_rows
        ]
        assert [r["priority"] for r in verb.value["items"]] == [
            r.priority for r in chat_rows
        ]

    def test_a_different_principals_alert_is_not_returned(self, durable):
        """The fix routes reads to a shared store; it must not widen who can
        read them."""
        durable.write_alert(recipient="@someone-else:site", summary="not yours")
        _alert(durable, "Rod A reactivity insertion 0.18 $/s")

        result = list_inbox.run({"recipient": RECIPIENT, "unread_only": True}, None)

        assert result.ok
        assert [r["summary"] for r in result.value["items"]] == [
            "Rod A reactivity insertion 0.18 $/s"
        ]
