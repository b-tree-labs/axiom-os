# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Spec-memory §3.7 session manager tests.

Sessions are the unit that scopes the ``Provenance.session_id`` slot.
This module owns:
- Session id + name shape (immutable id, renameable name)
- On-disk registry under ``$AXI_STATE_DIR/sessions/``
- Process-local "current session" resolution

Implementation lives in ``axiom.memory.session``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.memory import session as sess


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch):
    """Point session registry at a tmp directory and clear the
    process-local cache + the pytest auto-disable."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXI_DISABLE_SESSION", "0")
    monkeypatch.delenv("AXI_SESSION_ID", raising=False)
    monkeypatch.delenv("AXI_PRINCIPAL_ID", raising=False)
    sess.reset_for_tests()
    yield tmp_path
    sess.reset_for_tests()


class TestSessionIdentity:
    def test_create_session_returns_uri_id(self, tmp_state):
        meta = sess.create_session(principal_id="@u:x")
        assert meta.session_id.startswith(sess.SESSION_URI_PREFIX)

    def test_auto_name_format_is_cwd_dated(self, tmp_state, monkeypatch):
        # Ensure the named cwd basename is stable for the assertion.
        monkeypatch.chdir(tmp_state)
        meta = sess.create_session(principal_id="@u:x")
        # ``<cwd-basename>-<YYYY-MM-DD-HHMM>``
        parts = meta.name.rsplit("-", 4)
        assert parts[0] == tmp_state.name
        # YYYY portion of the date stamp:
        assert parts[1].isdigit() and len(parts[1]) == 4


class TestRegistryPersistence:
    def test_session_round_trips_through_disk(self, tmp_state):
        created = sess.create_session(principal_id="@u:x", name="rt-test")
        # Bypass the in-memory cache by re-reading from disk via resolve().
        loaded = sess.resolve(created.session_id)
        assert loaded is not None
        assert loaded.session_id == created.session_id
        assert loaded.name == "rt-test"

    def test_find_by_name(self, tmp_state):
        created = sess.create_session(principal_id="@u:x", name="findable")
        found = sess.find_by_name("findable")
        assert found is not None
        assert found.session_id == created.session_id

    def test_find_by_name_returns_none_when_unknown(self, tmp_state):
        assert sess.find_by_name("does-not-exist") is None

    def test_list_sessions_orders_by_last_active_desc(self, tmp_state):
        a = sess.create_session(principal_id="@u:x", name="older")
        # Force a later last_active_at on b.
        b = sess.create_session(principal_id="@u:x", name="newer")
        sess.touch(b.session_id)
        listed = sess.list_sessions(principal_id="@u:x")
        names = [m.name for m in listed]
        assert names.index("newer") < names.index("older")

    def test_list_sessions_filters_by_principal(self, tmp_state):
        sess.create_session(principal_id="@u:a", name="a1")
        sess.create_session(principal_id="@u:b", name="b1")
        ours = sess.list_sessions(principal_id="@u:a")
        assert {m.name for m in ours} == {"a1"}


class TestRename:
    def test_rename_changes_name_preserves_id(self, tmp_state):
        meta = sess.create_session(principal_id="@u:x", name="before")
        renamed = sess.rename(meta.session_id, "after")
        assert renamed.session_id == meta.session_id
        assert renamed.name == "after"
        # Old name no longer resolves.
        assert sess.find_by_name("before") is None
        assert sess.find_by_name("after") is not None

    def test_rename_rejects_collisions(self, tmp_state):
        meta_a = sess.create_session(principal_id="@u:x", name="taken")
        meta_b = sess.create_session(principal_id="@u:x", name="free")
        with pytest.raises(ValueError):
            sess.rename(meta_b.session_id, "taken")
        # Original names unchanged.
        assert sess.find_by_name("taken").session_id == meta_a.session_id

    def test_rename_unknown_raises(self, tmp_state):
        with pytest.raises(KeyError):
            sess.rename("session://does-not-exist", "anything")


class TestCurrentSessionResolution:
    def test_disabled_returns_none_and_empty_id(self, tmp_path, monkeypatch):
        # The pytest-context default disables session resolution.
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AXI_DISABLE_SESSION", "1")
        sess.reset_for_tests()
        assert sess.current_session() is None
        assert sess.current_session_id() == ""

    def test_pytest_context_default_disables_resolution(
        self, tmp_path, monkeypatch
    ):
        """PYTEST_CURRENT_TEST flips the default to disabled so the
        bare full-suite run does not auto-create a session for every test."""
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        # Do NOT set AXI_DISABLE_SESSION — rely on default.
        monkeypatch.delenv("AXI_DISABLE_SESSION", raising=False)
        sess.reset_for_tests()
        # In pytest, PYTEST_CURRENT_TEST is always set by the runner.
        assert sess.current_session_id() == ""

    def test_explicit_session_id_env_override(self, tmp_state):
        existing = sess.create_session(principal_id="@u:x", name="explicit")
        sess.reset_for_tests()
        # Drop the URI prefix to confirm both forms are accepted.
        bare = existing.session_id.removeprefix(sess.SESSION_URI_PREFIX)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("AXI_SESSION_ID", bare)
            assert sess.current_session_id() == existing.session_id

    def test_autobind_picks_recent_same_cwd_session(
        self, tmp_state, monkeypatch
    ):
        monkeypatch.chdir(tmp_state)
        principal = sess.current_principal_id()
        # Create a session with cwd_hint matching the current cwd.
        meta = sess.create_session(principal_id=principal, cwd=tmp_state)
        sess.reset_for_tests()
        # Auto-resume should pick it up.
        assert sess.current_session_id() == meta.session_id

    def test_autobind_skips_stale_sessions(self, tmp_state, monkeypatch):
        monkeypatch.chdir(tmp_state)
        principal = sess.current_principal_id()
        meta = sess.create_session(principal_id=principal, cwd=tmp_state)
        # Rewind last_active_at past the auto-resume window.
        stale = datetime.now(UTC) - timedelta(hours=24)
        on_disk = json.loads(sess._registry_path(meta.session_id).read_text())
        on_disk["last_active_at"] = stale.isoformat()
        sess._registry_path(meta.session_id).write_text(json.dumps(on_disk))
        sess._refresh_name_index()
        sess.reset_for_tests()
        # Auto-bind should reject the stale one and create a new session.
        active = sess.current_session_id()
        assert active != meta.session_id

    def test_use_session_rebinds_process(self, tmp_state):
        a = sess.create_session(principal_id="@u:x", name="alpha")
        b = sess.create_session(principal_id="@u:x", name="beta")
        sess.use_session("alpha")
        assert sess.current_session_id() == a.session_id
        sess.use_session(b.session_id)
        assert sess.current_session_id() == b.session_id

    def test_new_session_creates_and_rebinds(self, tmp_state):
        before = sess.current_session_id()
        meta = sess.new_session(name="fresh")
        assert sess.current_session_id() == meta.session_id
        assert sess.current_session_id() != before
