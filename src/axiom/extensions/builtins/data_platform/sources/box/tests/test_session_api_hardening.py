# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hardening tests for :class:`BoxSessionApiClient`.

Connector-hardening Day 1 (lakehouse epic #386). Tonight's DP-1 run
died at the first 429 because:

- the client raised a generic ``RuntimeError`` on 429 instead of a
  typed ``RateLimited`` carrying the window;
- there was no ``If-None-Match`` etag-skip, so every fetch paid the
  per-file token budget even for unchanged files;
- ``parse_headers`` was not wired into responses, so the client flew
  blind on ``X-RateLimit-Remaining``.

These tests pin the new behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.sources.box.session_api import (
    BoxSessionApiClient,
)
from axiom.infra.ratelimit import RateLimited


def _write_state(tmp_path: Path) -> Path:
    session_dir = tmp_path / "box"
    session_dir.mkdir()
    (session_dir / "state.json").write_text(
        json.dumps({"cookies": [{"name": "x", "value": "y",
                                 "domain": ".box.com", "path": "/"}],
                    "origins": []})
    )
    return session_dir


class _Resp:
    def __init__(self, status_code, headers=None, body=None, content=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.text = "" if body is None else json.dumps(body)
        self.content = content

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body if self._body is not None else {}


# -- rate-limit window stored on every response -------------------------------


def test_get_json_records_last_window_from_headers(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path)
    captured = {}

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True,
                 headers=None):
        captured["headers"] = headers
        return _Resp(200,
                     headers={"X-RateLimit-Limit": "1000",
                              "X-RateLimit-Remaining": "742"},
                     body={"hello": "world"})

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        c.get_json("/folders/1/items")
        assert c.last_window is not None
        assert c.last_window.limit == 1000
        assert c.last_window.remaining == 742


# -- 429 raises typed RateLimited carrying the window -------------------------


def test_get_json_429_raises_typed_RateLimited(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path)

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True,
                 headers=None):
        return _Resp(429, headers={"Retry-After": "30"})

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        with pytest.raises(RateLimited) as ei:
            c.get_json("/folders/1/items")
        assert ei.value.window.retry_after_s == 30


def test_get_bytes_429_raises_typed_RateLimited(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path)

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True,
                 headers=None):
        return _Resp(429, headers={"Retry-After": "5"})

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        with pytest.raises(RateLimited):
            c.get_bytes("/files/1/content")


# -- If-None-Match etag skip --------------------------------------------------


def test_get_json_sends_if_none_match_when_etag_given(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path)
    captured = {}

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True,
                 headers=None):
        captured["headers"] = headers or {}
        return _Resp(200, body={"ok": True})

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        c.get_json("/files/1", if_none_match="abc-etag-123")
    assert captured["headers"].get("If-None-Match") == "abc-etag-123"


def test_get_json_returns_none_on_304(tmp_path, monkeypatch):
    """304 Not Modified = caller's cached copy is still good; surface as None."""
    session_dir = _write_state(tmp_path)

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True,
                 headers=None):
        return _Resp(304, headers={})

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        result = c.get_json("/files/1", if_none_match="etag")
        assert result is None


def test_get_bytes_returns_empty_on_304(tmp_path, monkeypatch):
    session_dir = _write_state(tmp_path)

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True,
                 headers=None):
        return _Resp(304, content=b"")

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        result = c.get_bytes("/files/1/content", if_none_match="etag")
        assert result is None


# -- non-429 errors still raise RuntimeError ----------------------------------


def test_500_still_raises_runtime_error(tmp_path, monkeypatch):
    """Server errors are not rate-limit errors; preserve the existing shape."""
    session_dir = _write_state(tmp_path)

    def fake_get(self, url, params=None, timeout=None, allow_redirects=True,
                 headers=None):
        return _Resp(500, body={"err": "boom"})

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)

    with BoxSessionApiClient(session_dir=session_dir) as c:
        with pytest.raises(RuntimeError, match="500"):
            c.get_json("/folders/1/items")
