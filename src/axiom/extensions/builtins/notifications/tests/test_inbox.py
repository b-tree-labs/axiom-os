# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Inbox table read/write contract — TDD before implementation per CLAUDE.md."""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channels.inbox import (
    InboxChannelAdapter,
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.inbox import (
    InboxQuery,
    InMemoryInboxStore,
    list_unread,
    mark_read,
)
from axiom.governance import Classification


def test_empty_inbox_lists_nothing() -> None:
    store = InMemoryInboxStore()
    rows = list_unread(store, recipient="@jim:test")
    assert rows == []


def test_write_then_list() -> None:
    store = InMemoryInboxStore()
    row_id = store.write(
        recipient="@jim:test",
        receipt_id="rcpt-1",
        classification=Classification.INTERNAL,
        priority="normal",
        summary="hello",
    )
    rows = list_unread(store, recipient="@jim:test")
    assert len(rows) == 1
    assert rows[0].id == row_id
    assert rows[0].summary == "hello"


def test_mark_read_removes_from_unread() -> None:
    store = InMemoryInboxStore()
    row_id = store.write(
        recipient="@jim:test",
        receipt_id="rcpt-1",
        classification=Classification.INTERNAL,
        priority="normal",
        summary="hello",
    )
    mark_read(store, row_id=row_id)
    assert list_unread(store, recipient="@jim:test") == []


def test_query_filters_by_classification() -> None:
    store = InMemoryInboxStore()
    store.write(
        recipient="@jim:test",
        receipt_id="r1",
        classification=Classification.INTERNAL,
        priority="normal",
        summary="a",
    )
    store.write(
        recipient="@jim:test",
        receipt_id="r2",
        classification=Classification.REGULATED,
        priority="normal",
        summary="b",
    )
    q = InboxQuery(recipient="@jim:test",
                   max_classification=Classification.INTERNAL)
    rows = store.query(q)
    assert [r.summary for r in rows] == ["a"]


def test_inbox_adapter_writes_through_provider() -> None:
    provider = InboxChannelAdapterProvider()
    caps = provider.capabilities()
    assert caps.name == "inbox"
    assert caps.classification_ceiling is Classification.CONTROLLED

    store = InMemoryInboxStore()
    adapter: InboxChannelAdapter = provider.build({"store": store})
    result = adapter.deliver_sync(
        recipient="@jim:test",
        receipt_id="r1",
        classification=Classification.REGULATED,
        priority="normal",
        summary="audit needed",
    )
    assert result.ok is True
    rows = list_unread(store, recipient="@jim:test")
    assert len(rows) == 1
    assert rows[0].summary == "audit needed"
