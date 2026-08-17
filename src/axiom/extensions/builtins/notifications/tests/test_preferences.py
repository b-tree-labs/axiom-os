# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the recipient-preferences primitive."""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapterRegistry,
)
from axiom.extensions.builtins.notifications.channels.inbox import (
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.preferences import (
    InMemoryRecipientPreferenceStore,
    PostgresRecipientPreferenceStore,
    RecipientChannel,
    RecipientProfile,
    RecipientProfileRow,
    parse_channel_spec,
    resolve_recipient,
)
from axiom.extensions.builtins.notifications.send import Priority
from axiom.governance import Classification

# ---- RecipientProfile validation -------------------------------------------


def test_profile_requires_at_sign_recipient() -> None:
    with pytest.raises(ValueError, match="must start with '@'"):
        RecipientProfile(
            recipient="bbooth",
            channels=(RecipientChannel("inbox", "@bbooth"),),
        )


def test_profile_requires_at_least_one_channel() -> None:
    with pytest.raises(ValueError, match="at least one channel"):
        RecipientProfile(recipient="@bbooth", channels=())


def test_profile_accepts_minimal_valid_input() -> None:
    p = RecipientProfile(
        recipient="@bbooth",
        channels=(RecipientChannel("inbox", "@bbooth"),),
    )
    assert p.recipient == "@bbooth"
    assert len(p.channels) == 1


def test_recipient_channel_defaults_min_priority_low() -> None:
    c = RecipientChannel("slack", "#alerts")
    assert c.min_priority is Priority.LOW


# ---- resolve_recipient -----------------------------------------------------


def _registry_with_inbox() -> ChannelAdapterRegistry:
    reg = ChannelAdapterRegistry()
    reg.register(InboxChannelAdapterProvider(store=InMemoryInboxStore()))
    return reg


def test_resolve_filters_by_min_priority() -> None:
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(
            RecipientChannel("inbox", "@bbooth", min_priority=Priority.URGENT),
            RecipientChannel("inbox", "@bbooth", min_priority=Priority.LOW),
        ),
    )
    # First channel filtered (urgent floor > normal); second remains.
    prefs = resolve_recipient(
        profile, Classification.INTERNAL, Priority.NORMAL, _registry_with_inbox()
    )
    assert prefs.ordered_channels == ("inbox",)


def test_resolve_drops_channel_above_priority_floor() -> None:
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(
            RecipientChannel(
                "slack", "#alerts", min_priority=Priority.URGENT
            ),
            RecipientChannel("inbox", "@bbooth", min_priority=Priority.LOW),
        ),
    )
    prefs = resolve_recipient(
        profile, Classification.INTERNAL, Priority.NORMAL, _registry_with_inbox()
    )
    assert prefs.ordered_channels == ("inbox",)


def test_resolve_intersects_with_admitted_for() -> None:
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(
            RecipientChannel("slack", "#alerts"),
            RecipientChannel("inbox", "@bbooth"),
        ),
    )
    prefs = resolve_recipient(
        profile, Classification.INTERNAL, Priority.NORMAL, _registry_with_inbox()
    )
    assert prefs.ordered_channels == ("inbox",)


def test_resolve_preserves_declared_order() -> None:
    from axiom.extensions.builtins.notifications.channels.base import (
        ChannelCapabilities,
        Direction,
    )

    reg = _registry_with_inbox()

    class _Stub:
        name = "stub"

        def capabilities(self):
            return ChannelCapabilities(
                name="stub",
                direction=Direction.OUTBOUND,
                priority_levels=("low", "normal", "high", "urgent"),
                classification_ceiling=Classification.CONTROLLED,
                supports_threading=False,
                supports_acknowledge=False,
                delivery_sla_p95_ms=500,
            )

        def build(self, config):  # pragma: no cover
            raise NotImplementedError

    reg.register(_Stub())
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(
            RecipientChannel("stub", "x"),
            RecipientChannel("inbox", "@bbooth"),
        ),
    )
    prefs = resolve_recipient(
        profile, Classification.INTERNAL, Priority.NORMAL, reg
    )
    assert prefs.ordered_channels == ("stub", "inbox")


def test_resolve_empty_falls_back_to_inbox() -> None:
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(RecipientChannel("slack", "#alerts"),),
    )
    reg = ChannelAdapterRegistry()
    prefs = resolve_recipient(
        profile, Classification.INTERNAL, Priority.NORMAL, reg
    )
    assert prefs.ordered_channels == ("inbox",)


def test_resolve_admits_controlled_via_inbox_ceiling() -> None:
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(RecipientChannel("inbox", "@bbooth"),),
    )
    prefs = resolve_recipient(
        profile, Classification.CONTROLLED, Priority.URGENT, _registry_with_inbox()
    )
    assert prefs.ordered_channels == ("inbox",)


# ---- In-memory store CRUD --------------------------------------------------


def test_in_memory_store_put_get_roundtrip() -> None:
    store = InMemoryRecipientPreferenceStore()
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(
            RecipientChannel("slack", "#alerts"),
            RecipientChannel("inbox", "@bbooth"),
        ),
    )
    store.put(profile)
    assert store.get("@bbooth") == profile


def test_in_memory_store_get_missing_returns_none() -> None:
    store = InMemoryRecipientPreferenceStore()
    assert store.get("@nope") is None


def test_in_memory_store_put_overwrites() -> None:
    store = InMemoryRecipientPreferenceStore()
    a = RecipientProfile("@bbooth", (RecipientChannel("inbox", "@bbooth"),))
    b = RecipientProfile("@bbooth", (RecipientChannel("slack", "#x"),))
    store.put(a)
    store.put(b)
    assert store.get("@bbooth").channels[0].channel == "slack"


def test_in_memory_store_delete_returns_true_when_present() -> None:
    store = InMemoryRecipientPreferenceStore()
    store.put(RecipientProfile("@bbooth", (RecipientChannel("inbox", "@bbooth"),)))
    assert store.delete("@bbooth") is True
    assert store.get("@bbooth") is None


def test_in_memory_store_delete_returns_false_when_absent() -> None:
    store = InMemoryRecipientPreferenceStore()
    assert store.delete("@nope") is False


def test_in_memory_store_list_sorted() -> None:
    store = InMemoryRecipientPreferenceStore()
    store.put(RecipientProfile("@zed", (RecipientChannel("inbox", "@zed"),)))
    store.put(RecipientProfile("@alice", (RecipientChannel("inbox", "@alice"),)))
    names = [p.recipient for p in store.list()]
    assert names == ["@alice", "@zed"]


# ---- Postgres store CRUD (sqlite-backed in test) --------------------------


def _sqlite_session_cm():
    from sqlalchemy import MetaData
    from sqlalchemy.schema import CreateTable

    engine = create_engine("sqlite:///:memory:", future=True)
    # Clone the recipient_profiles table into a schema-free MetaData so
    # sqlite can issue its DDL — the rest of the notifications schema
    # uses schema-qualified ForeignKeys that sqlite cannot resolve.
    clean = MetaData()
    tbl = RecipientProfileRow.__table__.to_metadata(clean, schema=None)
    with engine.begin() as conn:
        conn.execute(CreateTable(tbl))
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    @contextlib.contextmanager
    def _cm():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    return _cm


def test_postgres_store_put_get_roundtrip() -> None:
    store = PostgresRecipientPreferenceStore(session_cm=_sqlite_session_cm())
    profile = RecipientProfile(
        recipient="@bbooth",
        channels=(
            RecipientChannel("slack", "#alerts", min_priority=Priority.HIGH),
            RecipientChannel("inbox", "@bbooth"),
        ),
    )
    store.put(profile)
    fetched = store.get("@bbooth")
    assert fetched is not None
    assert fetched.recipient == "@bbooth"
    assert fetched.channels[0].channel == "slack"
    assert fetched.channels[0].min_priority is Priority.HIGH
    assert fetched.channels[1].channel == "inbox"


def test_postgres_store_overwrite() -> None:
    store = PostgresRecipientPreferenceStore(session_cm=_sqlite_session_cm())
    a = RecipientProfile("@bbooth", (RecipientChannel("inbox", "@bbooth"),))
    b = RecipientProfile("@bbooth", (RecipientChannel("slack", "#x"),))
    store.put(a)
    store.put(b)
    assert store.get("@bbooth").channels[0].channel == "slack"


def test_postgres_store_delete() -> None:
    store = PostgresRecipientPreferenceStore(session_cm=_sqlite_session_cm())
    profile = RecipientProfile("@bbooth", (RecipientChannel("inbox", "@bbooth"),))
    store.put(profile)
    assert store.delete("@bbooth") is True
    assert store.get("@bbooth") is None
    assert store.delete("@bbooth") is False


def test_postgres_store_list() -> None:
    store = PostgresRecipientPreferenceStore(session_cm=_sqlite_session_cm())
    store.put(RecipientProfile("@zed", (RecipientChannel("inbox", "@zed"),)))
    store.put(RecipientProfile("@alice", (RecipientChannel("inbox", "@alice"),)))
    names = [p.recipient for p in store.list()]
    assert names == ["@alice", "@zed"]


# ---- parse_channel_spec ----------------------------------------------------


def test_parse_channel_spec_basic() -> None:
    channels = parse_channel_spec(
        "slack=#alerts,twilio-sms=+15125550100,email=ben@example.com,inbox"
    )
    assert [c.channel for c in channels] == [
        "slack", "twilio-sms", "email", "inbox",
    ]
    assert channels[0].address == "#alerts"
    assert channels[3].address == ""


def test_parse_channel_spec_priority_floor() -> None:
    channels = parse_channel_spec("twilio-sms=+15125550100@urgent")
    assert channels[0].min_priority is Priority.URGENT


def test_parse_channel_spec_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_channel_spec("")


def test_parse_channel_spec_bad_priority_raises() -> None:
    with pytest.raises(ValueError, match="unknown priority"):
        parse_channel_spec("email=x@bogus")


# ---- Postgres-mode integration (skipped without a live DB) -----------------
#
# These exercise the actual ADR-052 path — ``session_for("notifications")``
# against a real Postgres — instead of the in-process sqlite seam. They
# guard the HERALD-2a regression where prefs written from one process were
# invisible to the daemon, silently degrading routing to the inbox channel.


def _pg_available() -> bool:
    import os

    try:
        import psycopg2  # type: ignore

        url = os.environ.get(
            "AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db"
        )
        conn = psycopg2.connect(url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pg_only = pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable")


@pg_only
def test_postgres_store_roundtrip_against_real_pg() -> None:
    """``PostgresRecipientPreferenceStore`` round-trips via ``session_for``.

    The default ``session_cm=None`` path uses ``session_for("notifications")``;
    this asserts the search-path-scoped queries resolve to the schema's
    ``recipient_profiles`` table rather than failing with the
    "schema-hardcoded model can't find its table" symptom from HERALD-2a.
    """
    from axiom.extensions.builtins.notifications.preferences import (
        _try_build_postgres_default,
    )

    store = _try_build_postgres_default()
    assert store is not None, "expected Postgres store when AXIOM_DB_URL works"

    handle = "@prefs-roundtrip-pg"
    try:
        store.put(
            RecipientProfile(
                recipient=handle,
                channels=(
                    RecipientChannel(
                        "slack", "#alerts", min_priority=Priority.HIGH
                    ),
                    RecipientChannel("inbox", handle),
                ),
            )
        )
        fetched = store.get(handle)
        assert fetched is not None
        assert fetched.channels[0].channel == "slack"
        assert fetched.channels[0].min_priority is Priority.HIGH
        assert handle in [p.recipient for p in store.list()]
    finally:
        store.delete(handle)


@pg_only
def test_default_store_is_postgres_backed_when_db_reachable() -> None:
    """``default_store()`` must return a persistence-backed store, not in-memory.

    The HERALD-2a routing regression was rooted here: ``default_store()``
    silently returned ``InMemoryRecipientPreferenceStore``, so prefs
    written by ``axi notifications recipient set`` vanished the instant
    the CLI process exited and the daemon resolved every send to the
    inbox fallback.
    """
    from axiom.extensions.builtins.notifications.preferences import (
        PostgresRecipientPreferenceStore,
        default_store,
        set_default_store,
    )

    set_default_store(None)
    try:
        store = default_store()
        assert isinstance(store, PostgresRecipientPreferenceStore)
    finally:
        set_default_store(None)
