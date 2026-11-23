# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for #2224 (ADR-271): a reader of part of a site never sees the journal's
node-wide number.

The journal numbers every row from one identity shared by every lane and site. A reader who may
read only some logs would see it jump past rows they cannot read. A whole-site reader (every log
at every site in scope, by the one LD-1 check) gets today's responses; everyone else gets an opaque
``key`` per row, opaque cursors and stream ids, no audit chain number, and an attestation row's
per-log number. Rows LG-2224-A1 to A9; A10 is appkit's.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

fastapi = pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import registry  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.journal import follow, numbers, rights, service, writer  # noqa: E402
from axiom.extensions.builtins.journal.rows import Row  # noqa: E402
from axiom.governance import doors as g  # noqa: E402

from .test_j1 import journal_db  # noqa: E402,F401
from .test_ld1 import (  # noqa: E402,F401
    LOG,
    ME,
    SITE,
    TIMELINE_DOORS,
    _timeline_caller,
    restore_log,
    timeline,
    world,
)

# ruff: noqa: F811  (pytest fixtures imported from the J1 and LD-1 tests)

HIDDEN_LOG = "hidden_log"
AUDIT_SEQ = 987654


def _append(**kw) -> dict:
    base = {"lane": "attestations", "subject": f"attest.{LOG}.signed", "source": "test",
            "source_id": uuid.uuid4().hex, "at": datetime.now(UTC), "status": "ok",
            "summary": "a row", "actor": ME, "site": SITE}  # fmt: skip
    base.update(kw)
    return writer.append(Row(**base))


@pytest.fixture
def rows(timeline):
    """Beside the LD-1 world's row: an attestation row with its record's per-log number, and an
    audit row carrying the audit chain's number."""
    att = _append(summary="Round check signed again", logbook=LOG,
                  ref={"logbook": LOG, "seq": 12, "attestation_id": "rec-12", "digest": "d" * 64,
                       "entry_type": "round_check"})  # fmt: skip
    audit = _append(lane="tool_audit", subject="audit.attest.show", source="skill_audit",
                    source_id=str(AUDIT_SEQ), summary="attest.show (ok)",
                    ref={"chain": "actions", "seq": AUDIT_SEQ, "hash": "h" * 64},
                    link=f"audit://actions/{AUDIT_SEQ}")  # fmt: skip
    return {**timeline, "att": att, "audit": audit}


@pytest.fixture
def partial(rows):
    """A second log at the site that the signed-in person may not read (labelled above them): they
    become a partial reader without losing the log the rows are in."""
    book = registry.get(LOG)
    hidden = replace(book, id=HIDDEN_LOG, handle="hiddenlog", classification="regulated")
    registry.register(hidden)
    rights.forget_decisions()
    yield rows
    registry._logbooks.pop(HIDDEN_LOG, None)
    rights.forget_decisions()


def _get(env, url: str):
    c = env["journal"]
    c.cookies.clear()
    as_(c, OP1)
    return c.get(url)


def _sse(text: str) -> list[dict]:
    """Each frame of a stream as ``{id, event, data}``."""
    out = []
    for frame in (f for f in text.split("\n\n") if f.strip()):
        item: dict = {}
        for line in frame.split("\n"):
            if line.startswith("id: "):
                item["id"] = line[4:]
            elif line.startswith("event: "):
                item["event"] = line[7:]
            elif line.startswith("data: "):
                item["data"] = json.loads(line[6:])
        if item:
            out.append(item)
    return out


_DIGITS = re.compile(r"^-?\d+$")
_POSITIONS = ("next_cursor", "after", "resumed_at", "last")


def journal_numbers_in(body) -> list[str]:
    """Where a body carries the journal's number or an audit chain number: a row's ``seq``, a raw
    position, or an audit row's chain number."""
    found: list[str] = []

    def walk(v, path):
        if isinstance(v, dict):
            if "lane" in v and "seq" in v:
                found.append(f"{path}.seq")
            if v.get("lane") == "tool_audit":
                ref = v.get("ref") or {}
                if "seq" in ref or str(AUDIT_SEQ) in json.dumps(v):
                    found.append(f"{path} audit number")
            for k, x in v.items():
                if k in _POSITIONS and (isinstance(x, int) or (isinstance(x, str) and _DIGITS.match(x))):
                    found.append(f"{path}.{k}")
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")

    walk(body, "$")
    return found


# -- A1: a whole-site reader's responses are today's -----------------------------------------


@pytest.mark.acceptance("LG-2224-A1")
def test_lg2224_a1_a_whole_site_reader_gets_todays_list_and_stream(rows, restore_log):
    caller = _timeline_caller(ME, state=rows["state"])
    assert numbers.whole_site(caller) is True
    r = _get(rows, "/api/v1/journal?limit=2")
    assert r.status_code == 200, r.text
    expected = service.query(caller, limit=2)
    assert r.json() == json.loads(json.dumps(expected, default=str))
    body = r.json()
    assert all("seq" in x and "key" not in x and "number" not in x for x in body["rows"])
    assert body["next_cursor"] is None or _DIGITS.match(body["next_cursor"])
    first = rows["row"]["seq"]
    s = _get(rows, f"/api/v1/journal/stream?once=1&last_event_id={first - 1}")
    frames = _sse(s.text)
    assert frames[0]["event"] == "ready" and frames[0]["data"]["after"] == first - 1
    row_frames = [f for f in frames if f["event"] == "row"]
    assert [int(f["id"]) for f in row_frames] == [f["data"]["seq"] for f in row_frames]
    audit = next(f["data"] for f in row_frames if f["data"]["lane"] == "tool_audit")
    assert audit["ref"]["seq"] == AUDIT_SEQ and audit["link"] == f"audit://actions/{AUDIT_SEQ}"


# -- A2, A3, A4: a partial reader's rows -----------------------------------------------------


@pytest.mark.acceptance("LG-2224-A2")
@pytest.mark.acceptance("LG-2224-A3")
@pytest.mark.acceptance("LG-2224-A4")
def test_lg2224_a2_a4_a_partial_reader_gets_keys_and_per_log_numbers(partial, restore_log):
    caller = _timeline_caller(ME, state=partial["state"])
    assert numbers.whole_site(caller) is False
    r = _get(partial, "/api/v1/journal")
    assert r.status_code == 200, r.text
    body = r.json()
    assert journal_numbers_in(body) == []
    by_summary = {x["summary"]: x for x in body["rows"]}
    att = by_summary["Round check signed again"]
    label = registry.get(LOG).display
    assert att["number"] == {"n": 12, "logbook": LOG, "label": label, "text": f"#12 in {label}"}
    audit = by_summary["attest.show (ok)"]
    assert audit["number"] is None
    assert str(AUDIT_SEQ) not in json.dumps(audit)
    assert audit["link"] is None and audit["source_id"] is None and "seq" not in audit["ref"]
    # A key is stable: the same row has the same key on every read.
    again = {x["id"]: x["key"] for x in _get(partial, "/api/v1/journal").json()["rows"]}
    assert again == {x["id"]: x["key"] for x in body["rows"]}
    assert all(not _DIGITS.match(k) for k in again.values())


# -- A5, A6: opaque cursors that page and resume, and refuse what does not decode ------------


@pytest.mark.acceptance("LG-2224-A5")
def test_lg2224_a5_an_opaque_cursor_pages_exactly_as_the_raw_one(partial, restore_log):
    caller = _timeline_caller(ME, state=partial["state"])
    first = _get(partial, "/api/v1/journal?limit=1").json()
    token = first["next_cursor"]
    assert token and not _DIGITS.match(token)
    second = _get(partial, f"/api/v1/journal?limit=1&cursor={token}").json()
    raw = service.query(caller, limit=1)["next_cursor"]
    expected = service.query(caller, limit=1, cursor=raw)["rows"]
    assert [x["id"] for x in second["rows"]] == [x["id"] for x in expected]


@pytest.mark.acceptance("LG-2224-A5")
@pytest.mark.acceptance("LG-2224-A7")
def test_lg2224_a5_a7_the_stream_resumes_from_an_opaque_id(partial, restore_log):
    listed = _get(partial, "/api/v1/journal").json()["rows"]
    oldest = listed[-1]
    s = _get(partial, f"/api/v1/journal/stream?once=1&last_event_id={oldest['key']}")
    assert s.status_code == 200, s.text
    frames = _sse(s.text)
    assert frames[0]["event"] == "ready"
    assert frames[0]["data"]["after"] == oldest["key"]
    row_frames = [f for f in frames if f["event"] == "row"]
    assert [f["data"]["id"] for f in row_frames] == [x["id"] for x in reversed(listed[:-1])]
    assert all(f["id"] == f["data"]["key"] and not _DIGITS.match(f["id"]) for f in row_frames)
    assert journal_numbers_in([f["data"] for f in frames]) == []


@pytest.mark.acceptance("LG-2224-A7")
def test_lg2224_a7_a_gap_names_no_journal_number(partial, restore_log, monkeypatch):
    monkeypatch.setattr(follow, "PAGE", 1)
    monkeypatch.setattr(follow, "REPLAY_MAX", 1)
    listed = _get(partial, "/api/v1/journal").json()["rows"]
    s = _get(partial, f"/api/v1/journal/stream?once=1&last_event_id={listed[-1]['key']}")
    gaps = [f["data"] for f in _sse(s.text) if f["event"] == "gap"]
    assert gaps, s.text
    assert journal_numbers_in(gaps) == []
    assert numbers.position(gaps[0]["resumed_at"]) > numbers.position(gaps[0]["after"])


@pytest.mark.acceptance("LG-2224-A6")
@pytest.mark.parametrize("bad", ["12", "knotatoken", "k" + "A" * 22])
def test_lg2224_a6_a_cursor_that_does_not_decode_is_refused(partial, restore_log, bad):
    r = _get(partial, f"/api/v1/journal?cursor={bad}")
    assert (r.status_code, r.json()["code"]) == (400, "bad_cursor")
    s = _get(partial, f"/api/v1/journal/stream?once=1&last_event_id={bad}")
    assert (s.status_code, s.json()["code"]) == (400, "bad_cursor")


def test_a_token_round_trips_and_a_tampered_one_does_not(journal_cursor_vault):
    for n in (0, 1, 4488, 2**40):
        t = numbers.token(n)
        assert numbers.position(t) == n
        assert t == numbers.token(n)
    t = numbers.token(4488)
    # A middle character: the last of 22 carries padding bits, so changing it may decode the same.
    flipped = t[:6] + ("A" if t[6] != "A" else "B") + t[7:]
    with pytest.raises(numbers.BadCursor):
        numbers.position(flipped)


# -- the key: in the vault, never in the state dir, and fail closed without it ---------------


@pytest.mark.acceptance("LG-2224-A11")
def test_lg2224_a11_the_cursor_key_lives_in_the_vault_and_never_in_the_state_dir(
    partial, restore_log, journal_cursor_vault
):
    numbers.forget_key()
    listed = _get(partial, "/api/v1/journal?limit=1").json()
    assert listed["next_cursor"]
    assert list(journal_cursor_vault.values) == [numbers.VAULT_NAME]
    assert journal_cursor_vault.sets == 1, "created once, on first use"
    key = journal_cursor_vault.values[numbers.VAULT_NAME].decode()
    numbers.forget_key()
    _get(partial, f"/api/v1/journal?limit=1&cursor={listed['next_cursor']}")
    assert journal_cursor_vault.sets == 1, "read back, never made again"
    state = partial["state"]
    assert not (state / "journal" / "cursor.key").exists()
    for f in state.rglob("*"):
        if f.is_file():
            assert key not in f.read_text(encoding="utf-8", errors="replace"), f
    assert key not in json.dumps(listed)


@pytest.mark.acceptance("LG-2224-A11")
def test_lg2224_a11_without_the_vault_a_partial_reader_gets_no_positions(partial, restore_log):
    class Down:
        def exists(self, name):
            raise ConnectionError("vault sealed")

    numbers.set_store_factory(lambda: Down())
    r = _get(partial, "/api/v1/journal?limit=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert journal_numbers_in(body) == []
    assert body["next_cursor"] is None
    assert all(x["key"] is None for x in body["rows"])
    raw = _get(partial, "/api/v1/journal?cursor=12")
    assert (raw.status_code, raw.json()["code"]) == (400, "bad_cursor")
    tok = _get(partial, "/api/v1/journal?cursor=kAAAAAAAAAAAAAAAAAAAAAA")
    assert (tok.status_code, tok.json()["code"]) == (503, "cursor_unavailable")
    s = _get(partial, "/api/v1/journal/stream?once=1")
    assert all("id" not in f for f in _sse(s.text))


# -- the skills: the read skill and the CLI Tail ---------------------------------------------


@pytest.mark.acceptance("LG-2224-A2")
def test_lg2224_a2_the_read_skill_and_the_tail_carry_no_journal_number(partial, restore_log, monkeypatch):
    import io
    from contextlib import redirect_stdout

    from axiom.extensions.builtins.journal.skills import read as read_skill

    caller = _timeline_caller(ME, state=partial["state"])
    monkeypatch.setattr(rights, "caller_from_ctx", lambda ctx: caller)
    result = read_skill.query({"limit": 50}, None)
    assert result.ok, result.errors
    assert journal_numbers_in(result.value) == []
    out = io.StringIO()
    with redirect_stdout(out):
        tailed = follow.tail({"limit": 50}, None)
    assert tailed.ok, tailed.errors
    assert journal_numbers_in(tailed.value) == []
    printed = out.getvalue()
    label = registry.get(LOG).display
    assert f"#12 in {label}" in printed
    for row in service.query(caller, limit=50)["rows"]:
        assert f"#{row['seq']}" not in printed.replace(f"#12 in {label}", "")


# -- A8: the LD-1 matrix, generated per Timeline door and transport --------------------------

#: Every Timeline door and each transport it has; a transport this file cannot drive fails.
PARTIAL_ROWS = [pytest.param(d, t, id=f"{d.id}-{t}") for d in TIMELINE_DOORS for t in d.transports]


@pytest.mark.acceptance("LG-2224-A8")
@pytest.mark.parametrize(("door", "transport"), PARTIAL_ROWS)
def test_lg2224_a8_no_timeline_door_hands_a_partial_reader_the_journal_number(
    door, transport, partial, restore_log
):
    assert transport == g.HTTP, f"{door.id}: no driver for transport {transport!r}"
    path = door.route[1]  # type: ignore[index]
    if path.endswith("/stream"):
        listed = _get(partial, "/api/v1/journal").json()["rows"]
        r = _get(partial, path + f"?once=1&last_event_id={listed[-1]['key']}")
        frames = _sse(r.text)
        assert all(not _DIGITS.match(f.get("id", "x")) for f in frames)
        body = [f.get("data") for f in frames]
    else:
        r = _get(partial, path + "?limit=1")
        body = r.json()
    assert r.status_code == 200, r.text
    assert journal_numbers_in(body) == [], (door.id, transport)


@pytest.mark.acceptance("LG-2224-A8")
def test_lg2224_a8_the_matrix_covers_every_timeline_door():
    assert {d.id for d in TIMELINE_DOORS} >= {"journal.attestations", "journal.attestations_stream"}
    assert {(p.values[0].id, p.values[1]) for p in PARTIAL_ROWS} == {
        (d.id, t) for d in TIMELINE_DOORS for t in d.transports
    }
