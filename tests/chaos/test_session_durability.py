# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos: can a session survive a bad moment?

A conversation is the product here. Losing one is not a degraded experience,
it is the experience gone. Two failures are cheap to cause and were both
possible:

- a save interrupted part-way DESTROYED the session that was already on disk,
  because the write truncated the file before writing the new contents;
- two surfaces saving the same session raced with no atomicity, so a reader
  could observe a half-written file.

Both are the same root cause: the write was not atomic.
"""

from __future__ import annotations

import json
import threading

import pytest

from axiom.infra.orchestrator.session import Session, SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path)


def _session_with(text: str) -> Session:
    session = Session()
    session.add_message("user", text)
    return session


class TestAnInterruptedSaveDoesNotDestroyWhatWasThere:
    def test_a_crash_mid_save_leaves_the_previous_session_intact(
        self, store, monkeypatch
    ):
        """The failure that motivated this: `write_text` truncates first, so a
        process dying between truncate and write left an empty or partial file
        where a conversation used to be."""
        session = _session_with("the first turn")
        store.save(session)
        assert store.load(session.session_id) is not None

        session.add_message("assistant", "the second turn")

        real_replace = __import__("os").replace

        def explode(*args, **kwargs):
            raise OSError("disk full, mid-save")

        monkeypatch.setattr("os.replace", explode)
        with pytest.raises(OSError):
            store.save(session)
        monkeypatch.setattr("os.replace", real_replace)

        recovered = store.load(session.session_id)
        assert recovered is not None, "the interrupted save destroyed the session"
        assert recovered.messages[0].content == "the first turn"

    def test_no_partial_file_is_left_behind(self, store, monkeypatch):
        session = _session_with("a turn")
        store.save(session)

        def explode(*args, **kwargs):
            raise OSError("interrupted")

        monkeypatch.setattr("os.replace", explode)
        with pytest.raises(OSError):
            store.save(session)

        strays = [
            p
            for p in store._dir.iterdir()
            if p.is_file() and not p.name.endswith(".json")
        ]
        assert strays == [], f"temporary files left behind: {strays}"


class TestDurabilityAgainstMachineFailure:
    """What a unit test can and cannot say.

    `os.replace` is atomic, so the tests above cover a dying PROCESS. They say
    nothing about a dying MACHINE: the rename can reach the disk before the
    file's contents do, leaving the new name over empty blocks. `fsync` before
    the rename is what closes that, and no in-process test can prove it —
    a mutation sweep confirmed removing the fsync breaks nothing observable
    here.

    So this pins the CALL rather than the guarantee, and is honest about the
    difference. Deleting the fsync because "the tests still pass" is exactly
    the mistake this docstring exists to prevent.
    """

    def test_contents_are_flushed_before_the_rename(self, store, monkeypatch):
        import os as os_module

        order: list[str] = []
        real_fsync, real_replace = os_module.fsync, os_module.replace

        def traced_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def traced_replace(src, dst):
            order.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr("os.fsync", traced_fsync)
        monkeypatch.setattr("os.replace", traced_replace)
        store.save(_session_with("durable"))

        assert order == ["fsync", "replace"], (
            f"contents must reach disk before the name points at them: {order}"
        )


class TestConcurrentSaves:
    def test_a_reader_never_observes_a_half_written_session(self, store):
        """With an atomic replace, a reader sees either the old session or the
        new one — never a truncated file mid-flight."""
        session = _session_with("turn one")
        store.save(session)
        session_id = session.session_id

        errors: list[Exception] = []
        stop = threading.Event()

        def writer():
            for n in range(60):
                if stop.is_set():
                    return
                session.add_message("user", f"turn {n}")
                try:
                    store.save(session)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        def reader():
            for _ in range(200):
                if stop.is_set():
                    return
                try:
                    loaded = store.load(session_id)
                    if loaded is None:
                        errors.append(AssertionError("session vanished mid-write"))
                        return
                except json.JSONDecodeError as exc:
                    errors.append(exc)
                    return
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        stop.set()

        assert errors == [], f"a reader saw a torn write: {errors[:3]}"

    def test_concurrent_writers_leave_valid_json(self, store):
        """Last-write-wins is acceptable; unreadable is not."""
        session = _session_with("start")
        store.save(session)

        def writer(tag: str):
            for n in range(30):
                session.add_message("user", f"{tag}-{n}")
                store.save(session)

        threads = [
            threading.Thread(target=writer, args=(tag,)) for tag in ("a", "b", "c")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert store.load(session.session_id) is not None
