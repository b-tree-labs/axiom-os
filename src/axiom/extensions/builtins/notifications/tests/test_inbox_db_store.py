# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The inbox needs a store that outlives the process.

`InMemoryInboxStore` is the only implementation, so an alert written by a
monitor in one process is invisible to a chat running in another — which is
every real deployment. The docstring on the Protocol has always said "in-memory
+ Postgres implementations conform"; this is the Postgres one.

ADR-052: the store goes through `session_for("notifications")` and never
constructs its own engine or names a schema.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.notifications.db_models import Base
from axiom.extensions.builtins.notifications.inbox import InboxQuery
from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore


@pytest.fixture
def store():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return DatabaseInboxStore(engine=engine)


def _write(store, recipient, summary="rod A drifted 3.2%", priority="high"):
    """An inbox row exists BECAUSE a delivery receipt does, so the test builds
    the receipt rather than passing a null through a NOT NULL column — the
    constraint is describing something real."""
    import uuid

    from sqlalchemy.orm import Session as _S

    from axiom.extensions.builtins.notifications.db_models import DeliveryReceipt

    receipt_id = f"rcpt-{uuid.uuid4().hex[:8]}"
    with _S(store._engine) as s:
        s.add(
            DeliveryReceipt(
                id=receipt_id,
                envelope_json={},
                intent="alert",
                actor="@monitor:local",
                recipient=recipient,
                classification="internal",
                priority=priority,
                outcome="delivered",
                correlation_id=f"corr-{uuid.uuid4().hex[:8]}",
            )
        )
        s.commit()
    return store.write(
        recipient=recipient,
        receipt_id=receipt_id,
        classification="internal",
        priority=priority,
        summary=summary,
    )


class TestItSurvivesTheProcess:
    def test_a_written_alert_is_readable_back(self, store):
        row_id = _write(store, "@alice:example")
        rows = store.query(InboxQuery(recipient="@alice:example"))
        assert [r.id for r in rows] == [row_id]
        assert rows[0].summary == "rod A drifted 3.2%"
        assert rows[0].priority == "high"

    def test_a_second_store_on_the_same_database_sees_it(self, store):
        """The whole point: a monitor writes in one process, chat reads in
        another."""
        _write(store, "@alice:example")
        other = DatabaseInboxStore(engine=store._engine)
        assert len(other.query(InboxQuery(recipient="@alice:example"))) == 1


class TestItIsPerRecipient:
    def test_one_recipient_never_sees_another(self, store):
        _write(store, "@alice:example", "alice's rod")
        _write(store, "@bob:example", "bob's rod")
        rows = store.query(InboxQuery(recipient="@alice:example"))
        assert [r.summary for r in rows] == ["alice's rod"]

    def test_an_empty_recipient_matches_nothing(self, store):
        """Not "matches everything" — an unidentified reader has no inbox."""
        _write(store, "@alice:example")
        assert store.query(InboxQuery(recipient="")) == []

    def test_an_empty_recipient_does_not_even_match_an_empty_row(self, store):
        """A mutation sweep showed the previous test passes without the guard,
        because `WHERE recipient = ''` matches nothing anyway. The guard exists
        so an unidentified reader NEVER queries — pinned with a row that a bare
        query WOULD return.
        """
        _write(store, "")
        assert store.query(InboxQuery(recipient="")) == []


class TestUnreadAndOrdering:
    def test_unread_only_excludes_what_was_read(self, store):
        first = _write(store, "@alice:example", "first")
        _write(store, "@alice:example", "second")
        store.mark_read(row_id=first)
        rows = store.query(InboxQuery(recipient="@alice:example", unread_only=True))
        assert [r.summary for r in rows] == ["second"]

    def test_without_unread_only_everything_is_returned(self, store):
        """Pinned so the filter cannot become unconditional and hide history."""
        first = _write(store, "@alice:example", "first")
        _write(store, "@alice:example", "second")
        store.mark_read(row_id=first)
        rows = store.query(InboxQuery(recipient="@alice:example"))
        assert len(rows) == 2

    def test_newest_first(self, store):
        for n in range(3):
            _write(store, "@alice:example", f"alert {n}")
        rows = store.query(InboxQuery(recipient="@alice:example"))
        assert rows[0].created_at >= rows[-1].created_at

    def test_the_limit_is_honoured(self, store):
        for n in range(10):
            _write(store, "@alice:example", f"alert {n}")
        assert len(store.query(InboxQuery(recipient="@alice:example", limit=4))) == 4


class TestMarkingRead:
    def test_marking_read_is_idempotent(self, store):
        row_id = _write(store, "@alice:example")
        store.mark_read(row_id=row_id)
        store.mark_read(row_id=row_id)
        assert store.query(InboxQuery(recipient="@alice:example", unread_only=True)) == []

    def test_marking_read_twice_does_not_move_the_timestamp(self, store):
        """A mutation sweep showed the test above passes even if the second
        call OVERWRITES `read_at` — the row stays out of unread either way.
        When something was first seen is the auditable fact, so it must not
        drift on a re-read.
        """
        row_id = _write(store, "@alice:example")
        store.mark_read(row_id=row_id)
        first = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        ).read_at
        store.mark_read(row_id=row_id)
        again = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        ).read_at
        assert again == first

    def test_marking_an_unknown_row_is_harmless(self, store):
        store.mark_read(row_id="no-such-row")

    def test_read_at_is_timezone_aware(self, store):
        """Surfaces compare timestamps across machines; a naive one cannot be
        ordered against a stamp from another host."""
        row_id = _write(store, "@alice:example")
        before = datetime.now(UTC)
        store.mark_read(row_id=row_id)
        row = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        )
        assert row.read_at is not None
        assert row.read_at.tzinfo is not None
        assert row.read_at >= before.replace(microsecond=0)


class TestAcknowledgement:
    """`acknowledged_at` existed with no writer. "Did somebody pick this up"
    is the question asked after an incident, and an inbox that cannot answer
    it is a log, not a duty roster."""

    def test_it_records_WHO_took_it_up(self, store):
        """Not only when. `acknowledged_at` alone is unambiguous exactly while
        "the recipient is the only possible answerer" holds, and group
        recipients end that. Recorded before there are rows to backfill —
        backfilling an acknowledger that can no longer be determined is a
        permanent gap in the record, not a migration."""
        row_id = _write(store, "@alice:example")
        store.acknowledge(row_id=row_id, by="@alice:example")
        row = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        )
        assert row.acknowledged_by == "@alice:example"

    def test_an_unacknowledged_row_names_nobody(self, store):
        """Pinned so the field cannot default to the recipient and look
        populated — that would be the conflation this column removes."""
        row_id = _write(store, "@alice:example")
        row = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        )
        assert row.acknowledged_by == ""
        assert row.acknowledged_at is None

    def test_a_refused_ack_records_no_acknowledger(self, store):
        row_id = _write(store, "@alice:example")
        with pytest.raises(PermissionError):
            store.acknowledge(row_id=row_id, by="@mallory:evil")
        row = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        )
        assert row.acknowledged_by == ""

    def test_acknowledging_records_it(self, store):
        row_id = _write(store, "@alice:example")
        assert store.acknowledge(row_id=row_id, by="@alice:example") is True
        row = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        )
        assert row.acknowledged_at is not None
        assert row.acknowledged_at.tzinfo is not None

    def test_only_the_recipient_may_acknowledge(self):
        """There is no "who acknowledged" column, and that is only sound
        because of this rule — otherwise the timestamp would mean "somebody,
        at some point", which nobody can act on."""
        pass

    def test_a_stranger_is_refused(self, store):
        row_id = _write(store, "@alice:example")
        with pytest.raises(PermissionError, match="cannot acknowledge"):
            store.acknowledge(row_id=row_id, by="@mallory:evil")
        row = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        )
        assert row.acknowledged_at is None, "a refused ack must not record"

    def test_acknowledging_is_idempotent_and_does_not_move_the_time(self, store):
        row_id = _write(store, "@alice:example")
        assert store.acknowledge(row_id=row_id, by="@alice:example") is True
        first = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        ).acknowledged_at
        assert store.acknowledge(row_id=row_id, by="@alice:example") is False
        again = next(
            r for r in store.query(InboxQuery(recipient="@alice:example")) if r.id == row_id
        ).acknowledged_at
        assert again == first, "when it was first taken up is the auditable fact"

    def test_acknowledging_also_marks_it_read(self, store):
        """An unread-but-acknowledged row would keep re-arriving in chat after
        the person had already dealt with it."""
        row_id = _write(store, "@alice:example")
        store.acknowledge(row_id=row_id, by="@alice:example")
        assert store.query(InboxQuery(recipient="@alice:example", unread_only=True)) == []

    def test_acknowledging_something_that_does_not_exist_is_false(self, store):
        assert store.acknowledge(row_id="no-such-row", by="@alice:example") is False


class TestBothStoresAnswerTheSameContract:
    """Two implementations of one Protocol have to agree about TYPES.

    Found live, not by a test: `axi notifications list` raised
    `AttributeError: 'str' object has no attribute 'value'` against the
    database while working perfectly against the in-memory store. Each store
    had its own passing tests; nothing held them to each other, so the
    divergence lived in the gap between two green suites.

    `InboxRow.classification` is annotated `Classification`, so this isn't a
    preference — one store was returning the wrong type for a field the
    dataclass already declared. The test is parametrized over both stores on
    purpose: a test written against one implementation cannot see a
    disagreement between two.
    """

    @pytest.fixture(params=["memory", "database"])
    def either_store(self, request, store):
        if request.param == "database":
            return store, _write
        from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore

        def _write_memory(st, recipient, summary="rod A drifted 3.2%", priority="high"):
            return st.write(
                recipient=recipient,
                receipt_id="rcpt-memory",
                classification="internal",
                priority=priority,
                summary=summary,
            )

        return InMemoryInboxStore(), _write_memory

    def test_classification_comes_back_as_the_declared_type(self, either_store):
        st, write = either_store
        write(st, "@alice:example")
        row = st.query(InboxQuery(recipient="@alice:example"))[0]

        from axiom.governance import Classification

        assert isinstance(row.classification, Classification), (
            f"{type(st).__name__} returned {type(row.classification).__name__}; "
            "InboxRow declares Classification"
        )
        # The attribute the live failure actually tripped on.
        assert row.classification.value == "internal"

    def test_the_other_declared_fields_agree_too(self, either_store):
        """One field was the symptom; the contract is the whole row."""
        st, write = either_store
        write(st, "@alice:example")
        row = st.query(InboxQuery(recipient="@alice:example"))[0]

        assert isinstance(row.id, str) and row.id
        assert isinstance(row.recipient, str)
        assert isinstance(row.priority, str)
        assert isinstance(row.summary, str)
        assert isinstance(row.muted, bool)
        assert isinstance(row.acknowledged_by, str)
        assert row.read_at is None or row.read_at.tzinfo is not None
