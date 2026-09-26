# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for chat session persistence."""


from axiom.infra.orchestrator.session import Message, Session, SessionStore


class TestSession:
    """Test session data model."""

    def test_create_session(self):
        s = Session()
        assert s.session_id
        assert s.messages == []

    def test_add_message(self):
        s = Session()
        msg = s.add_message("user", "hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert len(s.messages) == 1
        assert s.updated_at  # Should be set

    def test_roundtrip(self):
        s = Session(context={"key": "val"})
        s.add_message("user", "hi")
        s.add_message("assistant", "hello!")

        d = s.to_dict()
        s2 = Session.from_dict(d)
        assert s2.session_id == s.session_id
        assert len(s2.messages) == 2
        assert s2.context == {"key": "val"}


class TestMessage:
    """Test message data model."""

    def test_roundtrip(self):
        m = Message(role="assistant", content="test", tool_calls=[{"name": "foo"}])
        d = m.to_dict()
        m2 = Message.from_dict(d)
        assert m2.role == "assistant"
        assert m2.content == "test"
        assert m2.tool_calls == [{"name": "foo"}]

    def test_tool_calls_optional(self):
        m = Message(role="user", content="hi")
        d = m.to_dict()
        assert "tool_calls" not in d  # Omitted when empty


class TestSessionStore:
    """Test session persistence."""

    def test_create_and_load(self, tmp_path):
        store = SessionStore(tmp_path / "sessions")
        session = store.create(context={"project": "example-consumer"})
        session.add_message("user", "hello")
        store.save(session)

        loaded = store.load(session.session_id)
        assert loaded is not None
        assert loaded.session_id == session.session_id
        assert len(loaded.messages) == 1
        assert loaded.context["project"] == "example-consumer"

    def test_load_nonexistent(self, tmp_path):
        store = SessionStore(tmp_path / "sessions")
        assert store.load("nonexistent") is None

    def test_list_sessions(self, tmp_path):
        store = SessionStore(tmp_path / "sessions")
        s1 = store.create()
        s1.add_message("user", "hello")
        store.save(s1)
        s2 = store.create()
        s2.add_message("user", "world")
        store.save(s2)

        ids = store.list_sessions()
        assert s1.session_id in ids
        assert s2.session_id in ids

    def test_list_empty(self, tmp_path):
        store = SessionStore(tmp_path / "sessions")
        assert store.list_sessions() == []
