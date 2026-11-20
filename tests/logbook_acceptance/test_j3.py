# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit J3, the Axiom half: Tail (filed as "Follow"), a live tail of a lane.

Design: axiom-appkit `docs/design/proposed-2026-11-11/tail/README.md` (approved 2026-11-11, master
design D48), acceptance LB-J3-A1 to A17; the web rows are tested in appkit. Here: access is checked
at subscribe time and on every labelled row (A7); redaction happens before the wire (A13); a slow
reader or a long absence never loses a row silently, and a gap beyond what the stream will replay
is said, not hidden (A9); one read is recorded per subscription with its range (J2, X2); and the CLI
prints the same columns, follows with ``-f``, prints a lost stream and refuses a lane the person
may not read (A17).
"""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.journal import api as journal_api  # noqa: E402
from axiom.extensions.builtins.journal import follow, rights, service, writer  # noqa: E402
from axiom.extensions.builtins.journal.rows import Row  # noqa: E402

from .test_j1 import bus, caller, journal_db, live  # noqa: E402,F401
from .test_j2 import OP1_HANDLE, _read, _stream_reads, app  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the J1 and J2 tests)
SECRET = "ghp_" + "A" * 36


def put(summary: str = "step 3 of 7: tests", *, lane: str = "platform", subject: str = "run.5402",
        classification: str = "internal", site: str | None = "site-a") -> dict:  # fmt: skip
    return writer.append(Row(lane=lane, subject=subject, source="test", source_id=uuid.uuid4().hex,
                             at=datetime.now(UTC), status="info", summary=summary,
                             actor="@runner:example", site=site,
                             classification=classification))  # fmt: skip


def rows_of(frames) -> list[dict]:
    return [
        json.loads(f[2][len("data: ") :]) for f in frames if len(f) == 3 and f[1] == "event: row"
    ]


# -- A7: access at subscribe time and on every labelled row ---------------------------------


@pytest.mark.acceptance("LB-J3-A7")
def test_lb_j3_a_lane_the_person_may_not_read_is_refused_at_subscribe_with_nothing_else(
    app, monkeypatch
):
    import httpx

    from axiom.extensions.builtins.attest.tests.test_api import BASE, OP1
    from axiom.extensions.builtins.http import ThreadedServer
    from axiom.webauth import SESSION_COOKIE
    from axiom.webauth.session import issue_session_token

    monkeypatch.setattr(rights, "_lane_decider", lambda c, lane: lane != "tool_audit")
    rights.forget_decisions()
    token = issue_session_token(OP1, issuer=BASE, amr=("pwd",))
    with ThreadedServer(app).serving() as srv:
        r = httpx.get(srv.base_url + "/api/v1/journal/stream?lanes=tool_audit",
                      cookies={SESSION_COOKIE: token}, timeout=5.0)  # fmt: skip
    assert r.status_code == 403
    assert r.json() == {"detail": "Not available to you.", "code": "not_available"}


@pytest.mark.acceptance("LB-J3-A7")
def test_lb_j3_a_row_that_fails_its_label_is_dropped_before_the_wire(app, monkeypatch):
    monkeypatch.setattr(journal_api, "HEARTBEAT_SECONDS", 0.05)
    first = put("visible before")
    put("above the ceiling", classification="regulated")
    put("another site", site="site-b")
    last = put("visible after")
    frames = _read(app, f"/api/v1/journal/stream?lanes=platform&last_event_id={first['seq'] - 1}&once=1",
                   until=lambda fs: False, seconds=3.0)  # fmt: skip
    assert [r["summary"] for r in rows_of(frames)] == ["visible before", "visible after"]
    assert rows_of(frames)[-1]["seq"] == last["seq"]


# -- A13: redaction before the wire ---------------------------------------------------------


@pytest.mark.acceptance("LB-J3-A13")
def test_lb_j3_a_secret_never_reaches_the_store_or_the_wire(live, app, monkeypatch):
    monkeypatch.setattr(journal_api, "HEARTBEAT_SECONDS", 0.05)
    stored = put(f"pushed with {SECRET} to the index")
    assert SECRET not in json.dumps(stored)
    assert stored["summary"] == "pushed with [redacted:github-token] to the index"
    frames = _read(app, f"/api/v1/journal/stream?lanes=platform&last_event_id={stored['seq'] - 1}&once=1",
                   until=lambda fs: False, seconds=3.0)  # fmt: skip
    assert SECRET not in json.dumps(frames)
    assert rows_of(frames)[0]["summary"] == "pushed with [redacted:github-token] to the index"


@pytest.mark.acceptance("LB-J3-A13")
def test_lb_j3_a_row_written_before_redaction_is_redacted_on_the_way_out():
    row = {"seq": 1, "lane": "platform", "summary": f"token {SECRET}", "actor": "a"}
    out = follow.shown(row)
    assert out["summary"] == "token [redacted:github-token]"
    assert row["summary"] == f"token {SECRET}", "the input is not changed"


# -- A9: backpressure and the gap -----------------------------------------------------------


@pytest.mark.acceptance("LB-J3-A9")
def test_lb_j3_resuming_after_more_rows_than_one_page_sends_every_row_in_order(app, monkeypatch):
    monkeypatch.setattr(follow, "PAGE", 7)
    first = put("row 0")
    for i in range(1, 30):
        put(f"row {i}")
    frames = _read(app, f"/api/v1/journal/stream?lanes=platform&last_event_id={first['seq'] - 1}&once=1",
                   until=lambda fs: False, seconds=4.0)  # fmt: skip
    assert [r["summary"] for r in rows_of(frames)] == [f"row {i}" for i in range(30)]


@pytest.mark.acceptance("LB-J3-A9")
def test_lb_j3_a_gap_beyond_what_the_stream_replays_is_said_not_hidden(app, monkeypatch):
    monkeypatch.setattr(follow, "PAGE", 5)
    monkeypatch.setattr(follow, "REPLAY_MAX", 10)
    first = put("row 0")
    for i in range(1, 25):
        put(f"row {i}")
    frames = _read(app, f"/api/v1/journal/stream?lanes=platform&last_event_id={first['seq'] - 1}&once=1",
                   until=lambda fs: False, seconds=4.0)  # fmt: skip
    gaps = [json.loads(f[1][len("data: ") :]) for f in frames if f[0] == "event: gap"]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["after"] == first["seq"] - 1
    sent = rows_of(frames)
    # After the gap, the newest REPLAY_MAX rows, contiguous to the end: a hole is said, never hidden.
    assert [r["summary"] for r in sent] == [f"row {i}" for i in range(15, 25)]
    assert gap["resumed_at"] == sent[0]["seq"]


@pytest.mark.acceptance("LB-J3-A9")
def test_lb_j3_a_slow_reader_loses_no_row_when_the_live_queue_overflows(app, monkeypatch):
    monkeypatch.setattr(journal_api, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(follow, "QUEUE_MAX", 3)

    def write_burst():
        time.sleep(0.4)
        for i in range(40):
            put(f"burst {i}")

    t = threading.Thread(target=write_burst)
    t.start()
    try:
        frames = _read(app, "/api/v1/journal/stream?lanes=platform",
                       until=lambda fs: len(rows_of(fs)) >= 40, seconds=8.0)  # fmt: skip
    finally:
        t.join()
    assert [r["summary"] for r in rows_of(frames)] == [f"burst {i}" for i in range(40)]


# -- forward paging is keyset and bounded ---------------------------------------------------


@pytest.mark.acceptance("LB-J3-A9")
def test_lb_j3_rows_after_a_sequence_are_keyset_paged_oldest_first(live):
    first = put("a")
    put("b")
    put("c")
    page = service.after(caller(), after=first["seq"] - 1, lanes=["platform"], limit=2)
    assert [r["summary"] for r in page["rows"]] == ["a", "b"]
    assert page["more"] is True
    rest = service.after(caller(), after=page["last"], lanes=["platform"], limit=2)
    assert [r["summary"] for r in rest["rows"]] == ["c"] and rest["more"] is False


# -- A17: the CLI ---------------------------------------------------------------------------


def _cli(argv, monkeypatch, tmp_path):
    from axiom.extensions.builtins.attest import log_cli  # the `log` noun (ADR-243, unit F5)

    monkeypatch.setattr(follow, "_state_dir", lambda: tmp_path)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = log_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.acceptance("LB-J3-A17")
def test_lb_j3_the_cli_prints_the_tail_columns(live, monkeypatch, tmp_path):
    monkeypatch.setattr(rights, "caller_from_ctx", lambda ctx: caller())
    put("step 3 of 7: tests", subject="run.5402")
    put("not this run", subject="run.9999")
    code, out, _ = _cli(["timeline", "--lane", "platform", "--run", "run.5402"], monkeypatch,
                        tmp_path)  # fmt: skip
    assert code == 0, out
    lines = [x for x in out.splitlines() if x.strip()]
    assert len(lines) == 1
    assert "platform" in lines[0] and "@runner:example" in lines[0]
    assert "step 3 of 7: tests" in lines[0] and "#" in lines[0]
    assert "not this run" not in out


@pytest.mark.acceptance("LB-J3-A17")
def test_lb_j3_log_tail_with_a_lane_is_the_journal_tail(live, monkeypatch, tmp_path):
    monkeypatch.setattr(rights, "caller_from_ctx", lambda ctx: caller())
    put("from the lane")
    code, out, _ = _cli(["tail", "--lane", "platform"], monkeypatch, tmp_path)
    assert code == 0 and "from the lane" in out


@pytest.mark.acceptance("LB-J3-A17")
def test_lb_j3_follow_prints_new_rows_and_records_one_read(live, monkeypatch, tmp_path):
    monkeypatch.setattr(rights, "caller_from_ctx", lambda ctx: caller())
    put("before")
    ticks = {"n": 0}

    def tick(seconds):
        ticks["n"] += 1
        if ticks["n"] == 1:
            put("after one tick")
        if ticks["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(follow, "_sleep", tick)
    code, out, _ = _cli(["timeline", "--lane", "platform", "-f"], monkeypatch, tmp_path)
    assert code == 0
    assert out.index("before") < out.index("after one tick")
    reads = _stream_reads(tmp_path)
    assert len(reads) == 1 and reads[0]["access"]["surface"] == "cli"
    assert reads[0]["access"]["lanes"] == ["platform"]


@pytest.mark.acceptance("LB-J3-A17")
def test_lb_j3_follow_prints_a_lost_stream_and_keeps_trying(live, monkeypatch, tmp_path):
    monkeypatch.setattr(rights, "caller_from_ctx", lambda ctx: caller())
    calls = {"n": 0}
    real = service.after

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("connection refused")
        if calls["n"] >= 4:
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(service, "after", flaky)
    monkeypatch.setattr(follow, "_sleep", lambda s: None)
    code, out, err = _cli(["timeline", "--lane", "platform", "-f"], monkeypatch, tmp_path)
    assert code == 0
    assert "stream lost, last seen" in err and "reconnecting" in err


@pytest.mark.acceptance("LB-J3-A17")
def test_lb_j3_the_cli_refuses_a_lane_the_person_may_not_read(live, monkeypatch, tmp_path):
    monkeypatch.setattr(rights, "caller_from_ctx", lambda ctx: caller())
    monkeypatch.setattr(rights, "_lane_decider", lambda c, lane: lane != "tool_audit")
    rights.forget_decisions()
    code, out, err = _cli(["timeline", "--lane", "tool_audit"], monkeypatch, tmp_path)
    assert code == 1
    assert "not available to you" in (out + err) and "tool_audit" not in (out + err)
