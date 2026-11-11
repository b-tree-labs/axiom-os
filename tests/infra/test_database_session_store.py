# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The database-backed session store, which is the one a second surface reaches.

It must satisfy the same contract as the file store, INCLUDING the ownership
boundary. A store that persists correctly and enforces ownership loosely is
worse than files: files at least never left the machine.

SQLite in memory here — local testing only, which is what a test is.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from axiom.extensions.builtins.chat.db_models import Base
from sqlalchemy.orm import Session as Session_

from axiom.infra.orchestrator.session import Session
from axiom.infra.orchestrator.session_db import DatabaseSessionStore


@pytest.fixture
def engine():
    """One in-memory database shared by the store and any direct assertion.

    `sqlite://` gives each CONNECTION its own database, so the store and a
    test inspecting its tables would otherwise look at two different ones.
    StaticPool keeps a single connection for both.
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def store(monkeypatch, engine):
    monkeypatch.setattr(
        "axiom.infra.orchestrator.session_db.node_legacy_handles", lambda: ["@laptop:person"]
    )
    monkeypatch.setattr(
        "axiom.infra.orchestrator.session_db.may_adopt",
        lambda p: p == "@a1b2c3:example",
    )
    return DatabaseSessionStore(engine=engine)


def _spoken(store, principal, text="hello"):
    s = Session(principal_id=principal)
    s.add_message("user", text)
    store.save(s)
    return s


class TestItRoundTrips:
    def test_a_saved_session_comes_back_whole(self, store):
        saved = _spoken(store, "@alice:site", "the ROM cutover")
        loaded = store.load(saved.session_id)
        assert loaded is not None
        assert loaded.session_id == saved.session_id
        assert loaded.messages[0].content == "the ROM cutover"
        assert loaded.principal_id == "@alice:site"

    def test_an_empty_session_is_not_persisted(self, store):
        """Same rule as the file store: an empty session is not history."""
        s = Session(principal_id="@alice:site")
        store.save(s)
        assert store.load(s.session_id) is None

    def test_saving_twice_updates_rather_than_duplicates(self, store):
        s = _spoken(store, "@alice:site", "first")
        s.add_message("user", "second")
        store.save(s)
        loaded = store.load(s.session_id)
        assert len(loaded.messages) == 2
        assert store.list_sessions(principal="@alice:site") == [s.session_id]


class TestTheOwnershipBoundaryIsTheSame:
    def test_another_principal_cannot_load_it(self, store):
        saved = _spoken(store, "@alice:site", "private")
        assert store.load(saved.session_id, principal="@bob:site") is None

    def test_the_owner_can(self, store):
        saved = _spoken(store, "@alice:site")
        assert store.load(saved.session_id, principal="@alice:site") is not None

    def test_an_unowned_session_stays_readable(self, store):
        saved = _spoken(store, "")
        assert store.load(saved.session_id, principal="@alice:site") is not None

    def test_this_node_s_legacy_session_is_adopted(self, store):
        saved = _spoken(store, "@laptop:person")
        assert store.load(saved.session_id, principal="@a1b2c3:example") is not None

    def test_a_stranger_naming_the_legacy_handle_is_refused(self, store):
        saved = _spoken(store, "@laptop:person")
        assert store.load(saved.session_id, principal="@mallory:evil") is None
        assert store.latest_for("@mallory:evil") is None

    def test_the_listing_respects_the_boundary(self, store):
        mine = _spoken(store, "@alice:site")
        _spoken(store, "@bob:site")
        assert store.list_sessions(principal="@alice:site") == [mine.session_id]


class TestResume:
    def test_latest_for_returns_the_most_recent(self, store):
        _spoken(store, "@alice:site", "older")
        newer = _spoken(store, "@alice:site", "newer")
        found = store.latest_for("@alice:site")
        assert found is not None and found.session_id == newer.session_id

    def test_latest_for_a_stranger_is_none(self, store):
        _spoken(store, "@alice:site")
        assert store.latest_for("@bob:site") is None

    def test_an_archived_session_is_not_the_latest(self, store):
        """Archiving is how someone says "not this one" — resume must respect
        it, or the surface reopens what they put away."""
        old = _spoken(store, "@alice:site", "put away")
        store.archive(old.session_id)
        assert store.latest_for("@alice:site") is None


class TestMessagesAreAppendedNotRewritten:
    """The reason this schema changed.

    Holding messages in the session's JSON rewrote the whole conversation on
    every turn — O(n) per turn, O(n-squared) over a session, measured at 108 KB
    per save by turn 200. Invisible against local disk; not against a network.
    """

    def test_saving_a_turn_inserts_one_row(self, store, engine):
        from sqlalchemy import func, select

        from axiom.extensions.builtins.chat.db_models import ChatMessage

        session = Session(principal_id="@a1b2c3:example")
        for turn in range(5):
            session.add_message("user", f"turn {turn}")
            store.save(session)

        with Session_(engine) as s:
            rows = s.execute(
                select(func.count()).select_from(ChatMessage)
            ).scalar_one()
        assert rows == 5, "messages were rewritten rather than appended"

    def test_resaving_the_same_session_adds_nothing(self, store, engine):
        from sqlalchemy import func, select

        from axiom.extensions.builtins.chat.db_models import ChatMessage

        session = Session(principal_id="@a1b2c3:example")
        session.add_message("user", "only turn")
        store.save(session)
        store.save(session)
        store.save(session)

        with Session_(engine) as s:
            rows = s.execute(
                select(func.count()).select_from(ChatMessage)
            ).scalar_one()
        assert rows == 1, "an unchanged save duplicated the conversation"

    def test_the_session_row_does_not_also_hold_the_messages(self, store, engine):
        """Two sources of truth would restore the rewrite and let them
        disagree."""
        from axiom.extensions.builtins.chat.db_models import ChatSession

        session = Session(principal_id="@a1b2c3:example")
        session.add_message("user", "a turn")
        store.save(session)

        with Session_(engine) as s:
            row = s.get(ChatSession, session.session_id)
            assert "messages" not in (row.payload or {})

    def test_a_long_conversation_round_trips_whole(self, store):
        session = Session(principal_id="@a1b2c3:example")
        for turn in range(50):
            session.add_message("user", f"question {turn}")
            session.add_message("assistant", f"answer {turn}")
        store.save(session)

        recovered = store.load(session.session_id, principal="@a1b2c3:example")
        assert len(recovered.messages) == 100
        assert recovered.messages[0].content == "question 0"
        assert recovered.messages[-1].content == "answer 49"


class TestOrderingSurvivesDisagreeingClocks:
    """Surfaces run on different machines. Ordering comes from the sequence,
    never from a wall clock."""

    def test_order_is_preserved_when_timestamps_are_identical(self, store):
        session = Session(principal_id="@a1b2c3:example")
        for turn in range(6):
            session.add_message("user", f"turn {turn}")
        for message in session.messages:
            message.timestamp = "2026-09-08T12:00:00+00:00"  # same instant
        store.save(session)

        recovered = store.load(session.session_id, principal="@a1b2c3:example")
        assert [m.content for m in recovered.messages] == [
            f"turn {n}" for n in range(6)
        ]

    def test_order_is_preserved_when_timestamps_run_backwards(self, store):
        """A surface with a skewed clock must not reorder the conversation."""
        session = Session(principal_id="@a1b2c3:example")
        for turn in range(5):
            session.add_message("user", f"turn {turn}")
        for offset, message in enumerate(session.messages):
            message.timestamp = f"2026-09-08T12:0{5 - offset}:00+00:00"
        store.save(session)

        recovered = store.load(session.session_id, principal="@a1b2c3:example")
        assert [m.content for m in recovered.messages] == [
            f"turn {n}" for n in range(5)
        ]

    def test_two_surfaces_cannot_silently_occupy_one_position(self, store, engine):
        """`(session_id, seq)` is the primary key, so a collision is loud.
        Silently interleaving two surfaces' turns would corrupt a
        conversation while looking successful."""
        import pytest as _pytest
        from sqlalchemy.exc import IntegrityError

        from axiom.extensions.builtins.chat.db_models import ChatMessage

        session = Session(principal_id="@a1b2c3:example")
        session.add_message("user", "first")
        store.save(session)

        with _pytest.raises(IntegrityError):
            with Session_(engine) as s:
                s.add(
                    ChatMessage(
                        session_id=session.session_id,
                        seq=0,  # already taken
                        role="user",
                        content="a second surface, same position",
                        timestamp="",
                    )
                )
                s.commit()


class TestPayloadFidelity:
    """The session row must carry every session-level field `to_dict()`
    produces, minus the messages. Building it by hand would drop whatever
    the dataclass gains next; this pins that it does not."""

    def test_the_payload_is_exactly_to_dict_without_messages(self, store, engine):
        from axiom.extensions.builtins.chat.db_models import ChatSession

        session = Session(principal_id="@a1b2c3:example", title="a title")
        session.context["user_identity"] = {"principal": "@a1b2c3:example"}
        session.usage = {"tokens": 42}
        session.add_message("user", "a turn")
        store.save(session)

        expected = session.to_dict()
        expected.pop("messages", None)

        with Session_(engine) as s:
            stored = dict(s.get(ChatSession, session.session_id).payload or {})
        assert stored == expected

    def test_saving_does_not_disturb_the_live_session(self, store):
        """Messages are swapped out during serialisation; they must come back.
        A caller handing us a session must not find it emptied."""
        session = Session(principal_id="@a1b2c3:example")
        session.add_message("user", "one")
        session.add_message("assistant", "two")
        store.save(session)
        assert [m.content for m in session.messages] == ["one", "two"]
