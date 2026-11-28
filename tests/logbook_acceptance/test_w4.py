# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit W4, the Axiom half: a heartbeat the browser can see.

Ticket: docs/working/logbook-units/W4.md in the product repo that holds the plan. The Log page
(axiom-appkit) supervises the live stream and says "Degraded" when it goes quiet (ADR-147). A
browser's ``EventSource`` never surfaces an SSE comment frame, and the stream's heartbeat was only
the comment ``: heartbeat``, so a stream that fell silent while its socket stayed open looked Live
for ever. These rows pin the named ``heartbeat`` event the page watches for: it rides beside the
comment at the same interval, carries the server time, has no ``id`` (so it never moves the resume
point), and reaches only a session authorised for the site, as every other frame does.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import api  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import BASE, OP1, as_, sign_round  # noqa: E402
from axiom.webauth import SESSION_COOKIE  # noqa: E402
from axiom.webauth.session import issue_session_token  # noqa: E402


def _frames(lines: list[str]) -> list[list[str]]:
    """Complete SSE frames: runs of lines each closed by a blank line (a trailing partial frame is
    left out, so a frame is never judged before its last line has arrived)."""
    frames: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line == "":
            if current:
                frames.append(current)
            current = []
        else:
            current.append(line)
    return frames


def _read_stream(client, url: str, *, until, seconds: float = 8.0) -> list[list[str]]:
    """Open the stream over a real server and read frames until ``until(frames)`` or a deadline."""
    import httpx

    from axiom.extensions.builtins.http import ThreadedServer

    token = issue_session_token(OP1, issuer=BASE, amr=("pwd",))
    lines: list[str] = []
    with ThreadedServer(client.app).serving() as srv:
        with httpx.stream(
            "GET", srv.base_url + url, cookies={SESSION_COOKIE: token}, timeout=httpx.Timeout(10.0)
        ) as r:
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith("text/event-stream")
            deadline = time.monotonic() + seconds
            for line in r.iter_lines():
                lines.append(line)
                if until(_frames(lines)) or time.monotonic() > deadline:
                    break
    return _frames(lines)


def _beats(frames: list[list[str]]) -> list[list[str]]:
    return [f for f in frames if f and f[0] == "event: heartbeat"]


@pytest.mark.acceptance("LB-W4-A1")
def test_lb_w4_a1_the_stream_names_its_heartbeat_beside_the_comment(client, monkeypatch):
    monkeypatch.setattr(api, "HEARTBEAT_SECONDS", 0.05)
    sign_round(client)
    frames = _read_stream(
        client,
        "/api/v1/attest/demo_log/stream",
        until=lambda fs: len(_beats(fs)) >= 2 and [": heartbeat"] in fs,
    )
    assert frames[0][0] == "event: ready", frames[:2]
    beats = _beats(frames)
    assert len(beats) >= 2, frames
    for beat in beats:
        # A named event with a small JSON body carrying the server time, and nothing else:
        # no `id`, so the browser's resume point (Last-Event-ID) never moves on a heartbeat.
        assert len(beat) == 2, beat
        assert not any(line.startswith("id:") for line in beat), beat
        assert beat[1].startswith("data: "), beat
        body = json.loads(beat[1][len("data: ") :])
        assert set(body) == {"at"}
        at = datetime.fromisoformat(body["at"])
        assert at.tzinfo is not None
    # The comment frame stays: proxies keep an idle connection open on it.
    assert [": heartbeat"] in frames


@pytest.mark.acceptance("LB-W4-A1")
def test_lb_w4_a1_once_and_last_event_id_replay_are_unchanged(client, monkeypatch):
    monkeypatch.setattr(api, "HEARTBEAT_SECONDS", 0.05)
    sign_round(client)
    sign_round(client)
    with client.stream(
        "GET", "/api/v1/attest/demo_log/stream?once=1", headers={"Last-Event-ID": "1"}
    ) as r:
        body = "".join(r.iter_text())
    assert "event: ready" in body
    assert "id: 2\nevent: signed" in body and "id: 1\n" not in body
    # `once=1` closes after the replay: no heartbeat of either kind is written.
    assert "heartbeat" not in body


@pytest.mark.acceptance("LB-W4-A1")
def test_lb_w4_a1_a_heartbeat_arrives_after_a_replay_and_the_record_keeps_its_id(
    client, monkeypatch
):
    monkeypatch.setattr(api, "HEARTBEAT_SECONDS", 0.05)
    sign_round(client)
    sign_round(client)
    frames = _read_stream(
        client,
        "/api/v1/attest/demo_log/stream?last_event_id=1",
        until=lambda fs: len(_beats(fs)) >= 1,
    )
    kinds = [f[0] for f in frames]
    assert kinds[0] == "event: ready"
    assert "id: 2" in kinds and "id: 1" not in kinds
    replayed = next(f for f in frames if f[0] == "id: 2")
    assert replayed[1] == "event: signed"
    assert kinds.index("id: 2") < kinds.index("event: heartbeat")


@pytest.mark.acceptance("LB-W4-A2")
def test_lb_w4_a2_the_heartbeat_reaches_only_an_authorised_session_for_the_site(client):
    assert client.get("/api/v1/attest/demo_log/stream?once=1").status_code == 401
    r = as_(client, OP1).get("/api/v1/attest/demo_log/stream?once=1&site=site-b")
    assert r.status_code == 403 and r.json()["code"] == "site"
    ok = as_(client, OP1).get("/api/v1/attest/demo_log/stream?once=1")
    assert ok.status_code == 200 and "event: ready" in ok.text
