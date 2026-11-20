# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A session capture that caught no login must fail loudly, not land an empty state.json (#358)."""

from __future__ import annotations

import stat

import pytest

from axiom.extensions.builtins.publishing.providers.storage import session_state as ss
from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
    BoxBrowserStorageProvider,
)
from axiom.extensions.builtins.publishing.providers.storage.onedrive_browser import (
    OneDriveBrowserStorageProvider,
)

EMPTY = {"cookies": [], "origins": []}
REAL = {"cookies": [{"name": "box_visitor_id", "value": "x", "domain": ".box.com", "path": "/"}], "origins": []}


class FakeContext:
    def __init__(self, state):
        self._state = state

    def storage_state(self, path=None):
        return self._state


def test_empty_state_is_refused_and_nothing_is_written(tmp_path):
    target = tmp_path / "box" / "state.json"
    with pytest.raises(ss.EmptySessionError, match="Log in"):
        ss.save_session_state(FakeContext(EMPTY), target, provider="Box")
    assert not target.exists()


def test_a_real_state_is_written_private(tmp_path):
    target = tmp_path / "box" / "state.json"
    ss.save_session_state(FakeContext(REAL), target, provider="Box")
    assert ss.session_file_has_state(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("cls", [BoxBrowserStorageProvider, OneDriveBrowserStorageProvider])
def test_a_leftover_empty_file_is_not_a_session(tmp_path, cls):
    d = tmp_path / "s"
    d.mkdir()
    (d / "state.json").write_text('{"cookies": [], "origins": []}')
    assert not cls({"session_dir": str(d)}).has_session()


def test_a_non_empty_file_is_a_session(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "state.json").write_text('{"cookies": [{"name": "a"}], "origins": []}')
    assert BoxBrowserStorageProvider({"session_dir": str(d)}).has_session()


def test_box_save_failure_names_the_cause(tmp_path):
    p = BoxBrowserStorageProvider({"session_dir": str(tmp_path / "s")})
    with pytest.raises(ss.EmptySessionError):
        p._save_session(FakeContext(EMPTY))
    assert not (tmp_path / "s" / "state.json").exists()


def test_verify_round_trips_users_me_before_writing(tmp_path):
    class Page:
        def __init__(self, ok):
            self.ok = ok

        def evaluate(self, js, *a):
            assert "/2.0/users/me" in js
            return self.ok

    p = BoxBrowserStorageProvider({"session_dir": str(tmp_path / "s"), "verify": True})
    with pytest.raises(RuntimeError, match="users/me"):
        p._save_session(FakeContext(REAL), page=Page(False))
    assert not (tmp_path / "s" / "state.json").exists()
    p._save_session(FakeContext(REAL), page=Page(True))
    assert p.has_session()
