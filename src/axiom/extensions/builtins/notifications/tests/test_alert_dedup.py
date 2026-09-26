# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A monitor's durable alert needs the same idempotency its non-durable sibling has.

`notifications send` dedups (a file log) but keeps its receipts in memory, so
nothing else can see what it delivered. `notifications alert` is durable but had
no dedup at all — so a nightly monitor whose condition stays true re-alerts every
single night, which is how an operator learns to ignore it.

A monitor author should not have to choose between "durable" and "quiet". The
`DedupLog` table has existed since migration 0001 with no writer; this is that
writer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.notifications.db_models import (
    Base,
    DedupLog,
    NotificationsInbox,
)
from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

KEY = "rod-noise:2026-09-10"


@pytest.fixture
def store():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return DatabaseInboxStore(engine=engine)


def _rows(store, recipient="@sam:netl"):
    with Session(store._engine) as s:
        return list(
            s.scalars(
                select(NotificationsInbox).where(
                    NotificationsInbox.recipient == recipient
                )
            )
        )


def test_same_dedup_key_lands_once(store):
    """The nightly re-run of a still-true rule must not make a second row."""
    first = store.write_alert(
        recipient="@sam:netl", summary="rod noise elevated", dedup_key=KEY
    )
    second = store.write_alert(
        recipient="@sam:netl", summary="rod noise elevated", dedup_key=KEY
    )

    assert first.deduplicated is False
    assert second.deduplicated is True
    # The caller still gets a usable handle — the one that actually exists.
    assert second.row_id == first.row_id
    assert second.receipt_id == first.receipt_id
    assert len(_rows(store)) == 1


def test_distinct_keys_both_land(store):
    """Negative control: suppression is keyed, not a blanket second-write block."""
    a = store.write_alert(
        recipient="@sam:netl", summary="day one", dedup_key="rod-noise:2026-09-10"
    )
    b = store.write_alert(
        recipient="@sam:netl", summary="day two", dedup_key="rod-noise:2026-09-11"
    )

    assert (a.deduplicated, b.deduplicated) == (False, False)
    assert len({a.row_id, b.row_id}) == 2
    assert len(_rows(store)) == 2


def test_dedup_is_scoped_per_actor(store):
    """Negative control: two monitors sharing a key are not each other's dedup.

    Without the actor in the key, one monitor's alert silently swallows another
    monitor's — a failure that looks exactly like "the rule never fired".
    """
    a = store.write_alert(
        recipient="@sam:netl", summary="from rod sentinel",
        actor="@rod-sentinel:netl", dedup_key=KEY,
    )
    b = store.write_alert(
        recipient="@sam:netl", summary="from fuel sentinel",
        actor="@fuel-sentinel:netl", dedup_key=KEY,
    )

    assert (a.deduplicated, b.deduplicated) == (False, False)
    assert len(_rows(store)) == 2


def test_no_dedup_key_never_suppresses(store):
    """Dedup is opt-in: a caller that passes no key keeps the old behaviour."""
    a = store.write_alert(recipient="@sam:netl", summary="one")
    b = store.write_alert(recipient="@sam:netl", summary="two")

    assert (a.deduplicated, b.deduplicated) == (False, False)
    assert len(_rows(store)) == 2


def test_expired_window_lets_the_next_one_through(store):
    """A condition that returns next month is news again, not a duplicate."""
    store.write_alert(
        recipient="@sam:netl", summary="rod noise elevated",
        dedup_key=KEY, dedup_ttl_seconds=3600,
    )
    with Session(store._engine) as s:
        row = s.scalars(select(DedupLog)).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()

    again = store.write_alert(
        recipient="@sam:netl", summary="rod noise elevated", dedup_key=KEY
    )

    assert again.deduplicated is False
    assert len(_rows(store)) == 2


def test_dedup_log_table_gets_its_writer(store):
    """The table has been in the schema since 0001 with nothing writing it."""
    written = store.write_alert(
        recipient="@sam:netl", summary="rod noise elevated",
        actor="@rod-sentinel:netl", dedup_key=KEY,
    )

    with Session(store._engine) as s:
        logged = s.scalars(select(DedupLog)).one()

    assert logged.primitive == "notifications"
    assert logged.actor == "@rod-sentinel:netl"
    assert logged.dedup_key == KEY
    assert logged.receipt_id == written.receipt_id
    assert logged.expires_at is not None


def test_suppressed_alert_does_not_write_a_second_receipt(store):
    """A receipt is a claim that something was delivered. Twice would be a lie."""
    from axiom.extensions.builtins.notifications.db_models import DeliveryReceipt

    store.write_alert(recipient="@sam:netl", summary="rod noise", dedup_key=KEY)
    store.write_alert(recipient="@sam:netl", summary="rod noise", dedup_key=KEY)

    with Session(store._engine) as s:
        assert len(list(s.scalars(select(DeliveryReceipt)))) == 1


def test_alert_cli_accepts_dedup_key_and_passes_it_through():
    """The flag has to survive argparse → params, or the store never sees it."""
    from axiom.extensions.builtins.notifications.cli import (
        _args_to_params,
        _build_parser,
    )

    args = _build_parser().parse_args(
        ["alert", "--recipient", "@sam:netl", "--summary", "rod noise",
         "--dedup-key", KEY]
    )

    assert _args_to_params(args)["dedup_key"] == KEY


def test_alert_cli_without_the_flag_sends_no_dedup_key():
    """Negative control: the param appears because the flag did, not always."""
    from axiom.extensions.builtins.notifications.cli import (
        _args_to_params,
        _build_parser,
    )

    args = _build_parser().parse_args(
        ["alert", "--recipient", "@sam:netl", "--summary", "rod noise"]
    )

    assert "dedup_key" not in _args_to_params(args)
