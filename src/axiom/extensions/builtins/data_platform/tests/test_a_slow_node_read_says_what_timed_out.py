# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A read the bound node is slow to answer says what timed out and the narrower question to ask.

Found in a colleague's first hour: a wide read gave up at 30 s with a raw ``TimeoutError``
traceback. It now says which read timed out, against which node, and which narrower question to
try (add a window, name a feed), and the wait is configurable (``AXIOM_DATA_READ_TIMEOUT``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from axiom.extensions.builtins.data_platform import read_target as rt

FACE = "https://node.example.org"


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(rt.READ_TIMEOUT_ENV, raising=False)
    (tmp_path / rt.STATE_FILENAME).write_text(json.dumps({
        "current": "alpha-site",
        "bindings": {"alpha-site": {"site": "alpha-site", "face_url": FACE}},
    }), encoding="utf-8")
    monkeypatch.setattr(rt, "holds_medallion", lambda: False)
    monkeypatch.setattr(rt, "_key", lambda _face: "k")
    waited: list = []
    return waited


class _SlowResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise TimeoutError("The read operation timed out")


@pytest.mark.parametrize("raised_at", ["read", "connect"])
def test_a_timeout_is_a_plain_sentence_with_the_narrower_question(bound, monkeypatch, raised_at):
    def urlopen(request, timeout=None):
        bound.append(timeout)
        if raised_at == "connect":
            raise urllib.error.URLError(TimeoutError("timed out"))
        return _SlowResponse()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result = rt.ask(rt.resolve_read_target(), verb="data.series", params={"table": "", "column": "x"})

    assert not result.ok
    (error,) = result.errors
    assert error.startswith(
        "timed out: alpha-site at https://node.example.org did not answer `data.series` within 30 s."
    )
    assert "add a window (--last 1h, or --start/--end)" in error
    assert "name one feed or channel" in error
    assert rt.READ_TIMEOUT_ENV in error
    assert "Traceback" not in error and "TimeoutError" not in error
    assert bound == [30.0]


def test_the_wait_is_configurable(bound, monkeypatch):
    monkeypatch.setenv(rt.READ_TIMEOUT_ENV, "90")

    def urlopen(request, timeout=None):
        bound.append(timeout)
        return _SlowResponse()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result = rt.ask(rt.resolve_read_target(), verb="data.aggregate", params={"start": "2026-10-01"})
    assert bound == [90.0]
    assert "within 90 s" in result.errors[0] and "`data.aggregate`" in result.errors[0]
    assert "narrow the window" in result.errors[0]


def test_a_nonsense_timeout_keeps_the_default(monkeypatch):
    monkeypatch.setenv(rt.READ_TIMEOUT_ENV, "soon")
    assert rt.read_timeout() == rt.DEFAULT_READ_TIMEOUT


def test_a_read_that_had_a_window_is_not_told_to_add_one(bound, monkeypatch):
    """Found live: `data series ... --start ... --end ...` timed out and was told to "add a window
    (--start/--end)". The CLI folds --start/--end into series' ``window`` dict, which the hint
    never looked at."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _SlowResponse())
    params = {"table": "signals", "column": "value", "bucket": "1 minute", "time_column": "ts",
              "window": {"column": "ts", "start": "2026-10-20", "end": "2026-10-27"}}
    (error,) = rt.ask(rt.resolve_read_target(), verb="data.series", params=params).errors
    assert "add a window" not in error
    assert "narrow the window" in error
    assert "add a feed filter" in error
    assert "coarser bucket" in error

    params["filter"] = "feed = 'one-feed'"
    (error,) = rt.ask(rt.resolve_read_target(), verb="data.series", params=params).errors
    assert "feed filter" not in error and "narrow the window" in error
