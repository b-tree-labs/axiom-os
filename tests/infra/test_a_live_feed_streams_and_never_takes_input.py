# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A local monitor page gets live updates and cannot change anything.

A producer writes its snapshot and its event log to disk; a separate process
serves a page and streams both over Server-Sent Events. That process answers
GET and nothing else, binds to loopback unless told otherwise in so many
words, and never reaches into the producer.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from axiom.infra.live_feed import EventLog, LiveFeed


def _start(feed: LiveFeed) -> str:
    threading.Thread(target=feed.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{feed.port}"


@pytest.fixture
def paths(tmp_path):
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps({"channels": {"TC1": {"value": 20.0}}}))
    return snap, tmp_path / "events.jsonl"


def test_the_page_the_snapshot_and_the_stream(paths):
    snap, events = paths
    log = EventLog(events)
    feed = LiveFeed(snapshot_path=snap, events_path=events,
                    pages={"/": (b"<html>hmi</html>", "text/html")}, port=0, poll_s=0.05)
    base = _start(feed)
    try:
        assert urllib.request.urlopen(base + "/").read() == b"<html>hmi</html>"
        assert json.loads(urllib.request.urlopen(base + "/snapshot").read())["channels"]["TC1"]["value"] == 20.0
        log.append("connect", "connected to the data intake")
        stream = urllib.request.urlopen(base + "/events", timeout=5)
        seen: list[str] = []
        deadline = time.time() + 5
        while time.time() < deadline and not ({"snapshot", "log"} <= set(seen)):
            line = stream.readline().decode().strip()
            if line.startswith("event: "):
                seen.append(line[len("event: "):])
        assert {"snapshot", "log"} <= set(seen)
    finally:
        feed.shutdown()


def test_nothing_but_get_is_answered(paths):
    snap, events = paths
    feed = LiveFeed(snapshot_path=snap, events_path=events, pages={}, port=0)
    base = _start(feed)
    try:
        req = urllib.request.Request(base + "/snapshot", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 405
    finally:
        feed.shutdown()


def test_it_will_not_listen_beyond_this_machine_unless_told_to(paths):
    snap, events = paths
    with pytest.raises(ValueError, match="loopback"):
        LiveFeed(snapshot_path=snap, events_path=events, pages={}, host="0.0.0.0", port=0)


def test_the_event_log_is_bounded_and_tails_from_an_offset(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", max_bytes=2000)
    for i in range(200):
        log.append("retry", f"attempt {i}", fix="wait")
    events, offset = log.tail(0)
    assert events and events[-1]["message"] == "attempt 199"
    assert (tmp_path / "e.jsonl").stat().st_size <= 4000
    log.append("connect", "back")
    more, _ = log.tail(offset, after=events[-1]["at"])
    assert [e["message"] for e in more] == ["back"]
