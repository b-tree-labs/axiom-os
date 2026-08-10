# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the thread data model + store.

Threads are 1:1 conversations between one student and the instructor.
Either party can open; either can reply; status moves open → answered
→ closed. Persisted as one JSON file per thread on the coordinator.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.classroom_threads import (
    Thread,
    ThreadMessage,
    ThreadStore,
    new_thread_id,
)

# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------


class TestThreadId:
    def test_ids_are_unique(self):
        ids = {new_thread_id() for _ in range(100)}
        assert len(ids) == 100

    def test_ids_are_url_safe(self):
        for _ in range(20):
            tid = new_thread_id()
            assert all(c.isalnum() or c in "_-" for c in tid), tid


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class TestShape:
    def test_thread_is_frozen(self):
        t = Thread(
            thread_id="t", classroom_id="c", student_id="s",
            opened_by="student", status="open",
            opened_at="2026-04-23", messages=[],
        )
        with pytest.raises((AttributeError, Exception)):
            t.status = "closed"

    def test_message_is_frozen(self):
        m = ThreadMessage(
            author_role="student", author_id="alice",
            text="hi", timestamp="2026-04-23",
        )
        with pytest.raises((AttributeError, Exception)):
            m.text = "changed"


# ---------------------------------------------------------------------------
# Store — save, load, list, update
# ---------------------------------------------------------------------------


class TestStoreSaveAndLoad:
    def test_save_and_get_roundtrip(self, tmp_path):
        store = ThreadStore(tmp_path)
        thread = Thread(
            thread_id="abc", classroom_id="NE101", student_id="alice@ut.edu",
            opened_by="student", status="open",
            opened_at="2026-04-23T10:00+00:00",
            messages=[ThreadMessage(
                author_role="student", author_id="alice@ut.edu",
                text="I'm stuck on control rods",
                timestamp="2026-04-23T10:00+00:00",
            )],
        )
        store.save(thread)
        loaded = store.get("abc")
        assert loaded == thread

    def test_get_unknown_returns_none(self, tmp_path):
        store = ThreadStore(tmp_path)
        assert store.get("nope") is None


class TestStoreListing:
    def _seed_threads(self, store: ThreadStore) -> None:
        # 2 threads for alice, 1 for bob, 1 closed for alice.
        store.save(Thread(
            thread_id="t1", classroom_id="NE101", student_id="alice@ut.edu",
            opened_by="student", status="open", opened_at="2026-04-23T10:00+00:00",
            messages=[],
        ))
        store.save(Thread(
            thread_id="t2", classroom_id="NE101", student_id="alice@ut.edu",
            opened_by="instructor", status="answered",
            opened_at="2026-04-22T10:00+00:00",
            messages=[],
        ))
        store.save(Thread(
            thread_id="t3", classroom_id="NE101", student_id="bob@ut.edu",
            opened_by="student", status="open", opened_at="2026-04-23T11:00+00:00",
            messages=[],
        ))
        store.save(Thread(
            thread_id="t4", classroom_id="NE101", student_id="alice@ut.edu",
            opened_by="student", status="closed",
            opened_at="2026-04-20T10:00+00:00",
            messages=[],
        ))

    def test_list_for_student(self, tmp_path):
        store = ThreadStore(tmp_path)
        self._seed_threads(store)
        alice_threads = store.list_for_student("alice@ut.edu")
        assert {t.thread_id for t in alice_threads} == {"t1", "t2", "t4"}

    def test_list_for_student_orders_newest_first(self, tmp_path):
        store = ThreadStore(tmp_path)
        self._seed_threads(store)
        alice_threads = store.list_for_student("alice@ut.edu")
        # t1 opened 2026-04-23, t2 on 04-22, t4 on 04-20
        ids = [t.thread_id for t in alice_threads]
        assert ids == ["t1", "t2", "t4"]

    def test_list_all_classroom(self, tmp_path):
        store = ThreadStore(tmp_path)
        self._seed_threads(store)
        all_t = store.list_all()
        assert len(all_t) == 4

    def test_list_open_excludes_closed_and_answered(self, tmp_path):
        store = ThreadStore(tmp_path)
        self._seed_threads(store)
        open_ones = store.list_open()
        assert {t.thread_id for t in open_ones} == {"t1", "t3"}


# ---------------------------------------------------------------------------
# Reply + status transitions
# ---------------------------------------------------------------------------


class TestReply:
    def test_reply_appends_message(self, tmp_path):
        store = ThreadStore(tmp_path)
        store.save(Thread(
            thread_id="t1", classroom_id="NE101", student_id="alice",
            opened_by="student", status="open",
            opened_at="2026-04-23T10:00+00:00",
            messages=[ThreadMessage(
                author_role="student", author_id="alice",
                text="I need help",
                timestamp="2026-04-23T10:00+00:00",
            )],
        ))
        updated = store.reply(
            "t1",
            ThreadMessage(
                author_role="instructor", author_id="@prof:ut",
                text="What have you tried?",
                timestamp="2026-04-23T11:00+00:00",
            ),
        )
        assert len(updated.messages) == 2
        assert updated.messages[1].text == "What have you tried?"

    def test_reply_persists(self, tmp_path):
        store = ThreadStore(tmp_path)
        store.save(Thread(
            thread_id="t1", classroom_id="NE101", student_id="alice",
            opened_by="student", status="open", opened_at="2026-04-23T10:00+00:00",
            messages=[],
        ))
        store.reply(
            "t1",
            ThreadMessage(
                author_role="instructor", author_id="p", text="reply",
                timestamp="2026-04-23T11:00+00:00",
            ),
        )
        # Fresh store — the reply is on disk.
        reloaded = ThreadStore(tmp_path).get("t1")
        assert reloaded is not None
        assert len(reloaded.messages) == 1

    def test_reply_by_instructor_marks_answered(self, tmp_path):
        """When the instructor responds to a student-opened question,
        the default transition is open → answered. A student can
        push it back to open by replying again."""
        store = ThreadStore(tmp_path)
        store.save(Thread(
            thread_id="t1", classroom_id="NE101", student_id="alice",
            opened_by="student", status="open",
            opened_at="2026-04-23T10:00+00:00", messages=[],
        ))
        updated = store.reply("t1", ThreadMessage(
            author_role="instructor", author_id="p",
            text="Here's a guiding question...",
            timestamp="2026-04-23T11:00+00:00",
        ))
        assert updated.status == "answered"

    def test_reply_by_student_marks_open_again(self, tmp_path):
        store = ThreadStore(tmp_path)
        store.save(Thread(
            thread_id="t1", classroom_id="NE101", student_id="alice",
            opened_by="student", status="answered",
            opened_at="2026-04-23T10:00+00:00",
            messages=[ThreadMessage(
                author_role="instructor", author_id="p",
                text="think about it",
                timestamp="2026-04-23T11:00+00:00",
            )],
        ))
        updated = store.reply("t1", ThreadMessage(
            author_role="student", author_id="alice",
            text="okay but what about X?",
            timestamp="2026-04-23T12:00+00:00",
        ))
        assert updated.status == "open"

    def test_reply_to_unknown_thread_raises(self, tmp_path):
        store = ThreadStore(tmp_path)
        with pytest.raises(KeyError):
            store.reply("never-seen", ThreadMessage(
                author_role="student", author_id="a", text="x",
                timestamp="2026-04-23",
            ))

    def test_close_marks_closed(self, tmp_path):
        store = ThreadStore(tmp_path)
        store.save(Thread(
            thread_id="t1", classroom_id="NE101", student_id="alice",
            opened_by="student", status="answered",
            opened_at="2026-04-23T10:00+00:00", messages=[],
        ))
        store.close_thread("t1")
        assert store.get("t1").status == "closed"


# ---------------------------------------------------------------------------
# Disk format
# ---------------------------------------------------------------------------


class TestDiskLayout:
    def test_one_json_per_thread(self, tmp_path):
        store = ThreadStore(tmp_path)
        store.save(Thread(
            thread_id="abc", classroom_id="NE101", student_id="alice",
            opened_by="student", status="open", opened_at="t",
            messages=[],
        ))
        path = tmp_path / "threads" / "abc.json"
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["thread_id"] == "abc"
