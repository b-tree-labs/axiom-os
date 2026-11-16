# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for TL1, the Timeline's Everyone / Mine control and an agent's Activity,
the server half.

Design: the web PRD ``docs/prds/prd-site-view.md`` section 21.3.1, "One Timeline" (approved
2026-11-12): the Timeline's controls are a lane, a level, Tail, and Everyone or Mine. Mine is what
the viewer asked for and what was done for them; an agent's Activity is the Timeline filtered to
that agent. The rows are in the journal design note, section 9:

- A1: ``mine`` returns the rows whose actor is the caller or that were done for the caller, and
  nobody else's.
- A2: ``mine`` composes with the lane and level filters and pages by keyset with no row repeated
  or lost.
- A3: ``mine`` never widens what a caller may see: a lane they may not read, another site, a row
  above their ceiling stay out.
- A4: ``agent`` returns the rows that agent did (its own handle, in any context).
- A5: the Tail (``after``, the stream, the CLI tail) takes the same filters.
- A6: the HTTP route, the CLI (``log timeline --mine``), the chat ability and the MCP tool are
  one skill with the same answer.
- A7: the row builders record who a row was done for, from the record's own keys.
"""

from __future__ import annotations

import io
import logging
import uuid
from contextlib import redirect_stdout
from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.journal import follow, rights, rows, service, writer  # noqa: E402
from axiom.extensions.builtins.journal.rows import Row  # noqa: E402
from axiom.extensions.builtins.journal.skills import bind_default  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.site_scope import SiteScope  # noqa: E402
from axiom.infra.skill_dispatch import CLI_SURFACE, MCP_SURFACE, invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402

from .test_j1 import bus, caller, journal_db, live  # noqa: E402,F401
from .test_j2 import OP1_HANDLE, _read, app  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the J1 and J2 tests)

ME = OP1_HANDLE
OTHER = "@op2:site-a"


def put(summary: str, *, actor: str | None = ME, lane: str = "platform", status: str = "info",
        for_: list[str] | None = None, site: str | None = "site-a",
        classification: str = "internal") -> dict:  # fmt: skip
    ref = {"for": for_} if for_ else {}
    return writer.append(Row(lane=lane, subject="test.thing", source="test",
                             source_id=uuid.uuid4().hex, at=datetime.now(UTC), status=status,
                             summary=summary, actor=actor, ref=ref, site=site,
                             classification=classification))  # fmt: skip


def summaries(result: dict) -> list[str]:
    return [r["summary"] for r in result["rows"]]


def ctx(state_dir, *, handle=ME, surface="mcp"):
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.tl1"),
        principal=PrincipalContext(handle=handle, posture="sso", assured=True), surface=surface,
    )  # fmt: skip


@pytest.mark.acceptance("LB-TL1-A1")
def test_lb_tl1_mine_is_what_i_did_and_what_was_done_for_me(live):
    put("I did this")
    put("someone else did this", actor=OTHER)
    put("an agent did this for me", actor="@tidy", for_=[ME])
    put("an agent did this for someone else", actor="@tidy", for_=[OTHER])
    put("nobody in particular", actor=None)
    me = caller(ME)
    got = service.query(me, mine=True)
    assert summaries(got) == ["an agent did this for me", "I did this"]
    assert service.query(caller(OTHER), mine=True)["rows"][0]["summary"] == (
        "an agent did this for someone else"
    )
    # Everyone is the default and unchanged.
    assert len(service.query(me)["rows"]) == 5


@pytest.mark.acceptance("LB-TL1-A2")
def test_lb_tl1_mine_composes_with_lane_and_level_and_pages_by_keyset(live):
    wanted = []
    for i in range(23):
        put(f"noise {i}", actor=OTHER)
        if i % 2 == 0:
            wanted.append(put(f"mine bad {i}", status="bad")["summary"])
        else:
            put(f"mine ok {i}", status="ok")
        put(f"mine elsewhere {i}", lane="commands", status="bad")
    me = caller(ME)
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        page = service.query(me, mine=True, lanes="platform", levels="bad", limit=5, cursor=cursor)
        seen.extend(summaries(page))
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == list(reversed(wanted))
    assert len(seen) == len(set(seen))
    with pytest.raises(ValueError):
        service.query(me, levels="loud")


@pytest.mark.acceptance("LB-TL1-A3")
def test_lb_tl1_mine_never_widens_access(live, monkeypatch):
    put("mine, readable")
    put("mine, a lane I may not read", lane="commands")
    put("mine, another site", site="site-b")
    put("mine, above my ceiling", classification="regulated")
    monkeypatch.setattr(rights, "_lane_decider", lambda c, lane: lane != "commands")
    rights.forget_decisions()
    got = service.query(caller(ME), mine=True)
    assert summaries(got) == ["mine, readable"]
    assert got["hidden"] == {"platform": 1}, "the row above the ceiling is counted, as today"


@pytest.mark.acceptance("LB-TL1-A4")
def test_lb_tl1_an_agents_activity_is_the_timeline_filtered_to_that_agent(live):
    put("tidy swept", actor="@tidy")
    put("tidy in a context", actor="@tidy:site-a")
    put("tidyish is another agent", actor="@tidyish")
    put("a person", actor=ME)
    me = caller(ME)
    assert summaries(service.query(me, agent="tidy")) == ["tidy in a context", "tidy swept"]
    assert summaries(service.query(me, agent="@tidy")) == ["tidy in a context", "tidy swept"]
    # composes with mine: what the agent did for me
    put("tidy for me", actor="@tidy", for_=[ME])
    assert summaries(service.query(me, agent="tidy", mine=True)) == ["tidy for me"]


@pytest.mark.acceptance("LB-TL1-A5")
def test_lb_tl1_the_tail_takes_the_same_filters(live, app, tmp_path, monkeypatch):
    start = put("before")["seq"]
    put("theirs", actor=OTHER)
    put("mine 1")
    put("tidy for me", actor="@tidy", for_=[ME])
    me = caller(ME)
    got = service.after(me, after=start, mine=True)
    assert summaries(got) == ["mine 1", "tidy for me"]
    assert summaries(service.after(me, after=start, agent="tidy")) == ["tidy for me"]
    # the stream replays with the filter
    frames = _read(app, f"/api/v1/journal/stream?lanes=platform&mine=1&last_event_id={start}&once=1",
                   until=lambda fs: False, seconds=3.0)  # fmt: skip
    import json

    sent = [
        json.loads(f[2][len("data: ") :]) for f in frames if len(f) == 3 and f[1] == "event: row"
    ]
    assert [r["summary"] for r in sent] == ["mine 1", "tidy for me"]
    # and the live path drops a row that is not mine
    keep = service.RowFilter.of(mine=True, agent=None, levels=None, caller=me)
    assert keep.matches({"actor": ME, "ref": {}, "status": "info"})
    assert not keep.matches({"actor": OTHER, "ref": {}, "status": "info"})
    assert keep.matches({"actor": "@tidy", "ref": {"for": [ME]}, "status": "info"})
    # the CLI tail
    monkeypatch.setattr(rights, "_scope_for", lambda handle: SiteScope(sites=frozenset({"site-a"})))
    c = ctx(tmp_path, surface="cli")
    out = io.StringIO()
    with redirect_stdout(out):
        r = follow.tail({"lanes": ["platform"], "mine": True, "limit": 10}, c)
    assert r.ok
    printed = out.getvalue()
    assert "mine 1" in printed and "tidy for me" in printed and "theirs" not in printed


@pytest.mark.acceptance("LB-TL1-A6")
def test_lb_tl1_one_skill_on_every_surface(live, app, tmp_path, monkeypatch):
    import httpx

    from axiom.extensions.builtins.attest.tests.test_api import BASE, OP1
    from axiom.extensions.builtins.http import ThreadedServer
    from axiom.webauth import SESSION_COOKIE
    from axiom.webauth.session import issue_session_token

    put("theirs", actor=OTHER)
    put("mine")
    put("tidy did it", actor="@tidy", status="warn")
    monkeypatch.setattr(rights, "_scope_for", lambda handle: SiteScope(sites=frozenset({"site-a"})))
    for surface in (MCP_SURFACE, CLI_SURFACE, "agent_tool"):
        c = ctx(tmp_path, surface=surface)
        r = invoke_capability(c.registry, "journal.query", {"mine": True}, c, surface=surface)
        assert r.ok and summaries(r.value) == ["mine"], surface
        r = invoke_capability(c.registry, "journal.query", {"agent": "tidy", "levels": ["warn"]}, c,
                              surface=surface)  # fmt: skip
        assert r.ok and summaries(r.value) == ["tidy did it"], surface
    token = issue_session_token(OP1, issuer=BASE, amr=("pwd",))
    with ThreadedServer(app).serving() as srv:
        got = httpx.get(srv.base_url + "/api/v1/journal?mine=1", cookies={SESSION_COOKIE: token},
                        timeout=5.0).json()  # fmt: skip
        by_agent = httpx.get(srv.base_url + "/api/v1/journal?agent=tidy&levels=warn",
                             cookies={SESSION_COOKIE: token}, timeout=5.0).json()  # fmt: skip
        bad = httpx.get(srv.base_url + "/api/v1/journal?levels=loud",
                        cookies={SESSION_COOKIE: token}, timeout=5.0)  # fmt: skip
    assert summaries(got) == ["mine"]
    assert summaries(by_agent) == ["tidy did it"]
    assert bad.status_code == 400
    # the CLI verb passes --mine and --agent through to the skill
    from axiom.extensions.builtins.attest import log_cli

    seen: dict = {}

    def fake_invoke(name, params, c):
        seen.update(name=name, params=params)
        from axiom.infra.skills import SkillResult

        return SkillResult(ok=True, value={})

    monkeypatch.setattr(log_cli, "_invoke", fake_invoke)
    assert log_cli._timeline(["--mine", "--agent", "tidy", "--level", "warn"]) == 0
    assert seen["name"] == "journal.tail"
    assert seen["params"]["mine"] is True and seen["params"]["agent"] == "tidy"
    assert seen["params"]["levels"] == ["warn"]


@pytest.mark.acceptance("LB-TL1-A7")
def test_lb_tl1_rows_record_who_they_were_done_for():
    draft = rows.from_draft_created({"logbook": "demo_log", "draft_id": "d1", "entry_type": "ROUND",
                                     "origin": "@clerk", "for_principal": "op1:site-a"})  # fmt: skip
    assert draft.ref["for"] == ["@op1:site-a"]
    led = rows.from_ledger_row({"id": "x", "ts": "2026-11-12T00:00:00+00:00", "agent": "tidy",
                                "op_class": "sweep", "name": "sweep", "candidate": "c",
                                "outcome": "proceeded",
                                "metadata": {"requested_by": "@op1:site-a"}})  # fmt: skip
    assert led.ref["for"] == ["@op1:site-a"]
    assert led.actor == "@tidy"
    gen = rows.from_event("schedule.run.complete", {"id": "r1", "requester": "@op1:site-a",
                                                     "on_behalf_of": "op2:site-a"})  # fmt: skip
    assert gen.ref["for"] == ["@op1:site-a", "@op2:site-a"]
    plain = rows.from_event("schedule.run.complete", {"id": "r2"})
    assert "for" not in plain.ref, "a row with no one to name keeps its shape"


@pytest.mark.acceptance("LB-TL1-A8")
def test_lb_tl1_the_node_says_which_row_filters_it_accepts(live):
    """The Timeline shows Everyone / Mine only when the node names the filter (appkit #189)."""
    report = service.lanes_report(caller())
    assert report["filters"] == ["mine", "agent", "levels"]
