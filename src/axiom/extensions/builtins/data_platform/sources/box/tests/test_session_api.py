# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :class:`BoxSessionApiClient` — the pure-Python
(no-Playwright) Box REST client used by the production daemon path.

The browser variant (``BoxBrowserApiClient``) needs Chromium + system
libs at runtime; the session variant only needs ``requests`` and the
already-captured cookies. These tests pin the cookie-loading + URL
construction behavior so the daemon image can stay slim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.sources.box.session_api import (
    BoxSessionApiClient,
)


def _write_state(tmp_path: Path, cookies: list[dict] | None = None) -> Path:
    session_dir = tmp_path / "box"
    session_dir.mkdir()
    (session_dir / "state.json").write_text(
        json.dumps({"cookies": cookies or [], "origins": []})
    )
    return session_dir


def test_open_loads_cookies_into_session(tmp_path):
    session_dir = _write_state(
        tmp_path,
        cookies=[
            {"name": "box_session", "value": "abc", "domain": ".box.com",
             "path": "/", "secure": True},
            {"name": "extra", "value": "xyz", "domain": ".box.com", "path": "/"},
        ],
    )
    with BoxSessionApiClient(session_dir=session_dir) as c:
        names = {ck.name for ck in c._session.cookies}
        assert "box_session" in names
        assert "extra" in names


def test_missing_state_raises_only_without_a_token(tmp_path):
    # SSO cookies are the ONLY auth here (no bearer, no jwt) → the missing
    # session file is a real, fatal misconfiguration.
    bad = tmp_path / "no-such"
    with pytest.raises(RuntimeError, match="No Box session"):
        BoxSessionApiClient(session_dir=bad)._open()


def test_bearer_token_does_not_require_sso_state(tmp_path):
    # A server token (BOX_DEVELOPER_TOKEN / CCG / OAuth / JWT) is sufficient on
    # its own — the SSO state.json must NOT be required when a token is present.
    # Regression: the Dagster box_corpus_sensor died with "No Box session"
    # despite a valid BOX_DEVELOPER_TOKEN because _open hard-required the file.
    bad = tmp_path / "no-such"  # deliberately does not exist
    c = BoxSessionApiClient(session_dir=bad, bearer_token="dev-token-xyz")
    c._open()  # must not raise
    assert c._session.headers["Authorization"] == "Bearer dev-token-xyz"
    c.close()


def test_env_developer_token_also_bypasses_sso(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_DEVELOPER_TOKEN", "env-tok")
    c = BoxSessionApiClient(session_dir=tmp_path / "no-such")
    c._open()  # must not raise
    assert c._session.headers["Authorization"] == "Bearer env-tok"
    c.close()


def test_get_json_hits_api_base_and_returns_dict(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path, cookies=[
        {"name": "box_session", "value": "abc", "domain": ".box.com", "path": "/"},
    ])

    captured = {}

    class _Resp:
        ok = True
        status_code = 200
        text = ""
        def json(self): return {"hello": "world"}

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True, headers=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp()

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        result = c.get_json("/folders/123/items", params={"limit": 100})
        assert result == {"hello": "world"}
        assert captured["url"] == "https://api.box.com/2.0/folders/123/items"
        assert captured["params"] == {"limit": 100}


def test_get_json_raises_on_http_error(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path, cookies=[
        {"name": "box_session", "value": "abc", "domain": ".box.com", "path": "/"},
    ])

    class _Resp:
        ok = False
        status_code = 401
        text = "unauthorized"
        def json(self):
            return {}

    import requests
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, params=None, timeout=None, allow_redirects=True, headers=None: _Resp())

    with BoxSessionApiClient(session_dir=session_dir) as c:
        with pytest.raises(RuntimeError, match="401"):
            c.get_json("/folders/123/items")


def test_get_bytes_returns_content(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path, cookies=[
        {"name": "box_session", "value": "abc", "domain": ".box.com", "path": "/"},
    ])

    class _Resp:
        ok = True
        status_code = 200
        text = ""
        content = b"hello bytes"

    import requests
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, params=None, timeout=None, allow_redirects=True, headers=None: _Resp())

    with BoxSessionApiClient(session_dir=session_dir) as c:
        b = c.get_bytes("/files/777/content")
        assert b == b"hello bytes"


def test_path_normalization_adds_leading_slash(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path, cookies=[
        {"name": "box_session", "value": "abc", "domain": ".box.com", "path": "/"},
    ])
    captured = {}

    class _Resp:
        ok = True
        status_code = 200
        text = ""
        def json(self):
            return {}

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True, headers=None):
        captured["url"] = url
        return _Resp()

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        c.get_json("folders/9/items")  # no leading slash
        assert captured["url"] == "https://api.box.com/2.0/folders/9/items"


def test_bearer_token_sets_authorization_header(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path, cookies=[
        {"name": "x", "value": "y", "domain": ".box.com", "path": "/"},
    ])
    monkeypatch.setenv("BOX_DEVELOPER_TOKEN", "tok_xyz_123")

    with BoxSessionApiClient(session_dir=session_dir) as c:
        assert c._session.headers.get("Authorization") == "Bearer tok_xyz_123"


def test_explicit_bearer_overrides_env(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path, cookies=[
        {"name": "x", "value": "y", "domain": ".box.com", "path": "/"},
    ])
    monkeypatch.setenv("BOX_DEVELOPER_TOKEN", "env_token")

    with BoxSessionApiClient(session_dir=session_dir, bearer_token="explicit_token") as c:
        assert c._session.headers.get("Authorization") == "Bearer explicit_token"
