# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ADR-162 watcher primitive: cursor, debounce, content-identity dedup.

The primitive is pure mechanism. These tests pin the three behaviours every
instance inherits — a mirror and its origin collapse on SHA, the cursor
advances to the newest observation, and a too-soon poll is debounced and
reported as *not polled* (never as "no changes").
"""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.infra.watcher import (
    FileWatcherStore,
    Watcher,
    WatcherState,
    WatchItem,
)


def _commit(sha: str, updated_at: str) -> WatchItem:
    return WatchItem(
        identity=sha,
        kind="scm.push",
        payload={"sha": sha, "updated_at": updated_at},
    )


class TestContentIdentityDedup:
    def test_a_commit_seen_through_origin_and_mirror_collapses_to_one(self):
        # The same SHA arrives from two sources in one batch (origin + mirror).
        batch = [
            _commit("aaa", "2026-10-06T10:00:00Z"),
            _commit("bbb", "2026-10-06T11:00:00Z"),
            _commit("aaa", "2026-10-06T10:00:00Z"),  # mirror copy of aaa
        ]
        w = Watcher(name="repo-x", fetch=lambda since: batch)
        result = w.poll(WatcherState())
        assert result.polled is True
        assert {i.identity for i in result.items} == {"aaa", "bbb"}
        assert result.deduped == 1

    def test_first_seen_wins(self):
        batch = [
            WatchItem(identity="aaa", kind="scm.push", payload={"src": "origin"}),
            WatchItem(identity="aaa", kind="scm.push", payload={"src": "mirror"}),
        ]
        w = Watcher(name="repo-x", fetch=lambda since: batch)
        items = w.poll(WatcherState()).items
        assert len(items) == 1
        assert items[0].payload["src"] == "origin"


class TestCursorAdvance:
    def test_cursor_moves_to_the_newest_observation(self):
        batch = [
            _commit("aaa", "2026-10-06T10:00:00Z"),
            _commit("bbb", "2026-10-06T12:30:00Z"),
            _commit("ccc", "2026-10-06T11:00:00Z"),
        ]
        w = Watcher(name="repo-x", fetch=lambda since: batch)
        result = w.poll(WatcherState())
        assert result.state.cursor == "2026-10-06T12:30:00Z"

    def test_the_fetch_is_handed_the_prior_cursor(self):
        seen_since: list[str | None] = []

        def fetch(since):
            seen_since.append(since)
            return []

        w = Watcher(name="repo-x", fetch=fetch)
        state = WatcherState(cursor="2026-10-01T00:00:00Z")
        w.poll(state)
        assert seen_since == ["2026-10-01T00:00:00Z"]

    def test_an_empty_delta_leaves_the_cursor_where_it_was(self):
        # Advancing to `now` would skip the window (newest-seen, now]; a
        # cursor only ever moves forward to a *seen* observation.
        w = Watcher(name="repo-x", fetch=lambda since: [])
        now = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
        result = w.poll(WatcherState(cursor="2026-10-01T00:00:00Z"), now=now)
        assert result.polled is True
        assert result.state.cursor == "2026-10-01T00:00:00Z"

    def test_the_cursor_never_moves_backward(self):
        # A stray old observation cannot drag the high-water mark back.
        batch = [_commit("old", "2020-01-01T00:00:00Z")]
        w = Watcher(name="repo-x", fetch=lambda since: batch)
        result = w.poll(WatcherState(cursor="2026-10-01T00:00:00Z"))
        assert result.state.cursor == "2026-10-01T00:00:00Z"


class TestDebounce:
    def test_a_too_soon_poll_is_skipped_and_reported_as_not_polled(self):
        calls = {"n": 0}

        def fetch(since):
            calls["n"] += 1
            return []

        w = Watcher(name="repo-x", fetch=fetch, debounce_seconds=900)
        now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
        state = WatcherState(cursor="c", last_polled_at="2026-10-06T11:59:00Z")
        result = w.poll(state, now=now)
        assert result.polled is False
        assert result.items == []
        assert result.state == state  # unchanged
        assert calls["n"] == 0  # fetch not even called

    def test_a_poll_past_the_window_runs(self):
        w = Watcher(name="repo-x", fetch=lambda since: [], debounce_seconds=900)
        now = datetime(2026, 10, 6, 12, 30, 0, tzinfo=UTC)
        state = WatcherState(last_polled_at="2026-10-06T12:00:00Z")
        assert w.poll(state, now=now).polled is True


class TestPersistence:
    def test_state_round_trips_through_the_file_store(self, tmp_path):
        store = FileWatcherStore(tmp_path / "watchers")
        assert store.load("repo-x") == WatcherState()  # absent → empty
        state = WatcherState(cursor="2026-10-06T12:00:00Z", last_polled_at="2026-10-06T12:00:01Z")
        store.save("repo-x", state)
        assert store.load("repo-x") == state

    def test_a_name_with_separators_is_safe_on_disk(self, tmp_path):
        store = FileWatcherStore(tmp_path / "watchers")
        store.save("gitlab:group/repo", WatcherState(cursor="c"))
        assert store.load("gitlab:group/repo").cursor == "c"
