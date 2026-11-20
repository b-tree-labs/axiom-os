# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit J2, the Axiom half: the journal's live stream as the Timeline and
the Tail block read it.

Ticket: docs/working/logbook-units/J2.md in the product repo that holds the plan, with the Tail
design (master design D48, approved 2026-11-11). The Timeline (axiom-appkit) follows the stream in
its live mode and must never draw a stale stream as live, so the stream names its heartbeat the
way the attest stream does since unit W4 (a browser cannot see an SSE comment). Opening the stream
is one read of the journal and is recorded once on the action audit chain, with its range (the
lanes, the subject, the sequence it resumed after) and never a row's content (unit X2's rule).
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")

import fastapi  # noqa: E402

from axiom.extensions.builtins.attest.tests.test_api import BASE, OP1  # noqa: E402
from axiom.extensions.builtins.fleet import api as fleet_api  # noqa: E402
from axiom.extensions.builtins.http.authz_hook import build_session_resolver  # noqa: E402
from axiom.extensions.builtins.journal import api as journal_api  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.webauth import SESSION_COOKIE  # noqa: E402
from axiom.webauth.keys import reset_key_store_for_tests  # noqa: E402
from axiom.webauth.session import issue_session_token  # noqa: E402

from .test_j1 import bus, journal_db, live  # noqa: E402,F401


@pytest.fixture
def app(live, tmp_path, monkeypatch):  # noqa: F811
    reset_key_store_for_tests()
    fleet_api.set_session_resolver_factory(lambda: build_session_resolver(issuer=BASE))
    monkeypatch.setenv(journal_api.INGEST_ENV, "0")
    monkeypatch.setattr(journal_api, "_state_dir", lambda: tmp_path)
    a = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    journal_api.register_routes(router)
    a.include_router(router)
    yield a
    fleet_api.reset_session_resolver_factory()
    reset_key_store_for_tests()


def _frames(lines: list[str]) -> list[list[str]]:
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


def _read(app, url: str, *, until, seconds: float = 8.0) -> list[list[str]]:
    import httpx

    from axiom.extensions.builtins.http import ThreadedServer

    token = issue_session_token(OP1, issuer=BASE, amr=("pwd",))
    lines: list[str] = []
    with ThreadedServer(app).serving() as srv:
        with httpx.stream("GET", srv.base_url + url, cookies={SESSION_COOKIE: token},
                          timeout=httpx.Timeout(10.0)) as r:  # fmt: skip
            assert r.status_code == 200, r.text
            deadline = time.monotonic() + seconds
            for line in r.iter_lines():
                lines.append(line)
                if until(_frames(lines)) or time.monotonic() > deadline:
                    break
    return _frames(lines)


def _stream_reads(state_dir) -> list[dict]:
    chain = audit_trail.read_chain(audit_trail.audit_path(state_dir))
    return [r for r in chain if r.get("skill") == "journal.read.stream"]


@pytest.mark.acceptance("LB-J2-A2")
def test_lb_j2_the_journal_stream_names_its_heartbeat(app, monkeypatch):
    monkeypatch.setattr(journal_api, "HEARTBEAT_SECONDS", 0.05)
    frames = _read(
        app,
        "/api/v1/journal/stream",
        until=lambda fs: sum(f[0] == "event: heartbeat" for f in fs) >= 2,
    )
    assert frames[0][0] == "event: ready"
    beats = [f for f in frames if f[0] == "event: heartbeat"]
    assert len(beats) >= 2
    for beat in beats:
        assert len(beat) == 2 and not any(line.startswith("id:") for line in beat)
        assert set(json.loads(beat[1][len("data: ") :])) == {"at"}
    assert [": heartbeat"] in frames


@pytest.mark.acceptance("LB-J2-A3")
def test_lb_j2_opening_the_stream_is_one_recorded_read_with_its_range(app, tmp_path, monkeypatch):
    monkeypatch.setattr(journal_api, "HEARTBEAT_SECONDS", 0.05)
    _read(app, "/api/v1/journal/stream?lanes=platform&subject=made_up&last_event_id=7",
          until=lambda fs: any(f[0] == "event: heartbeat" for f in fs))  # fmt: skip
    reads = _stream_reads(tmp_path)
    assert len(reads) == 1, reads
    r = reads[0]
    assert r["principal"] == OP1_HANDLE
    assert r["access"]["what"] == "stream" and r["access"]["surface"] == "web"
    assert r["access"]["lanes"] == ["platform"] and r["access"]["subject"] == "made_up"
    assert r["access"]["after"] == 7
    # A digest of the range, never a row's content.
    assert "rows" not in r and "summary" not in json.dumps(r["access"])


@pytest.mark.acceptance("LB-J2-A3")
def test_lb_j2_a_recording_failure_never_fails_the_stream(app, monkeypatch):
    def broken(**_kw):
        raise OSError("disk full")

    monkeypatch.setattr(audit_trail, "record_invocation", broken)
    monkeypatch.setattr(journal_api, "HEARTBEAT_SECONDS", 0.05)
    frames = _read(
        app, "/api/v1/journal/stream", until=lambda fs: any(f[0] == "event: heartbeat" for f in fs)
    )
    assert frames[0][0] == "event: ready"


OP1_HANDLE = "@op1:site-a"
