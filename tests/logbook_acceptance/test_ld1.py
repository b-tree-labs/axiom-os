# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-1: the cross-door access conformance matrix (#1758, ADR-251 D8
and D9).

One registry of doors (``axiom.governance.doors``, the log's rows in ``attest/doors.py``), one
check, one read record. The matrix below is generated from the registry and the reason enum:
every door, every transport, every reason. A door registered without a driver here fails the
build (LD-1-A3), and a route under ``/api/v1`` in a home that serves records that is neither a
door nor declared not to be one fails the router walk (LD-1-A1).

Each reason is arranged the same way for every door: one world, one record, one log, changed
only as the reason needs (the caller, the log's declaration, the roles, GUARD).
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import doors as log_doors  # noqa: E402
from axiom.extensions.builtins.attest import registry, roles  # noqa: E402
from axiom.extensions.builtins.attest.skills import DOOR_SKILLS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import _round_check, _sign  # noqa: E402
from axiom.extensions.builtins.journal import rights  # noqa: E402
from axiom.extensions.builtins.search import engine as search_engine  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.governance.doors import Reason  # noqa: E402
from axiom.infra import audit_trail, site_authority  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.webauth.users import User  # noqa: E402

from .test_j1 import journal_db  # noqa: E402,F401

SITE = "site-a"
LOG = "demo_log"
ME = "@op1:site-a"
OTHER = "@op2:site-a"
SECRET_NOTE = "ld1 note nobody may read twice"
OUTSIDER = User(user_id="op9", email="op9@example.org", name="Op Nine", roles=("user",),
                site="site-b", attributes={"idp": "entra"})  # fmt: skip
#: Content that must never reach a read record.
CONTENT = (SECRET_NOTE, "Round check", "walkdown")

HTTP_DOORS = tuple(d for d in g.doors() if g.HTTP in d.transports and d.shape == "target")
SKILL_DOORS = tuple(d for d in g.doors() if d.skill)
LISTING_DOORS = tuple(d for d in g.doors() if d.shape == "listing")

#: The matrix rows, generated: every target door × its transports × every reason (LD-1-A3).
ROWS = [
    pytest.param(d, t, r, id=f"{d.id}-{t}-{r.value}")
    for d in g.doors() if d.shape == "target"
    for t in d.transports
    for r in Reason
]  # fmt: skip


# -- the world ------------------------------------------------------------------------------


@pytest.fixture
def world(client, attest_db, node_signer, tmp_path, monkeypatch):
    """One signed record in the demo log at site-a, one export of it, a decided action pointing
    at it; the HTTP client's state dir is the skills' state dir, so one chain holds every read."""
    from axiom.extensions.builtins.attest import approvals, signing

    audit_trail._reset_for_tests()
    rights.forget_decisions()
    signing.set_provider(lambda: node_signer)
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    import axiom.infra.paths as paths

    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    rec = _sign(_round_check(note=SECRET_NOTE), node_signer).record
    export = as_(client, OP1).post(f"/api/v1/attest/{LOG}/exports?site={SITE}",
                                   json={"formats": ["text"]})  # fmt: skip
    assert export.status_code == 200, export.text

    class _Gate:
        def get(self, action_id):
            return object() if action_id == "act-1" else None

    monkeypatch.setattr(approvals, "gate", lambda: _Gate())
    monkeypatch.setattr(approvals, "decision_view", lambda _a: {"record": {
        "seq": rec["seq"], "site_id": SITE, "logbook": LOG, "attestation_id": rec["attestation_id"]}})  # fmt: skip
    yield {"client": client, "rec": rec, "export_id": export.json()["export_id"],
           "state": tmp_path}  # fmt: skip
    audit_trail._reset_for_tests()


def _book():
    return registry.get(LOG)


def arrange(reason: Reason | None, monkeypatch, state: Path) -> dict:
    """Change the world just enough for one reason. Returns who asks: an HTTP user (None is no
    session), a skill handle (None is nobody), and the site the skill caller's key is bound to."""
    from axiom.extensions.builtins.attest import access_requests

    who = {"user": OP1, "handle": ME, "bound": None}
    book = _book()
    # The caller cannot already know of the log (no listing, no link): the 404 that names nothing.
    # The knowable variant (403 no_access with how to ask, #1867) is test_access_request's.
    monkeypatch.setattr(access_requests, "knowable", lambda *_a, **_k: False)
    if reason is Reason.UNRESOLVED_CALLER:
        who.update(user=None, handle=None)
        # A terminal whose operator cannot be named either.
        monkeypatch.setattr(site_authority, "_asking_principal", lambda: "")
    elif reason is Reason.SITE:
        who.update(user=OUTSIDER, bound="site-b")
    elif reason is Reason.POLICY_UNAVAILABLE:
        def unreachable(*_a, **_k):
            raise ConnectionError("GUARD is unreachable")

        monkeypatch.setattr(site_authority, "decide_site_read", unreachable)
    elif reason is Reason.NOT_PERMITTED:
        registry.register(replace(book, readers=("auditor",)))
        who.update(user=OP2, handle=OTHER)
    elif reason is Reason.REVOKED:
        registry.register(replace(book, readers=("auditor",)))
        roles.grant(OTHER, SITE, "auditor", by=ME, state_dir=state)
        roles.revoke(OTHER, SITE, "auditor", by=ME, state_dir=state)
        who.update(user=OP2, handle=OTHER)
    elif reason is Reason.UNLABELED:
        monkeypatch.delitem(registry._logbooks, LOG)  # the record stays; its declaration is gone
    elif reason is Reason.CLASSIFICATION:
        registry.register(replace(book, classification="regulated"))
    elif reason is Reason.EXPORT_CONTROL:
        registry.register(replace(book, export_controlled=True))
    return who


@pytest.fixture
def restore_log():
    book = registry.get(LOG)
    yield
    registry.register(book)


# -- the drivers: one per transport, so a door is reached by its registration alone ----------


def _url(door: g.Door, w: dict) -> str:
    method, path = door.route  # type: ignore[misc]
    url = path.format(logbook=LOG, attestation_id=w["rec"]["attestation_id"],
                      export_id=w["export_id"], action_id="act-1", sha256="0" * 64,
                      form="no_such_form")  # fmt: skip
    url += f"?site={SITE}"
    if path.endswith("/stream"):
        url += "&once=1"
    return url


def drive_http(door: g.Door, w: dict, who: dict):
    c = w["client"]
    c.cookies.clear()
    if who["user"] is not None:
        as_(c, who["user"])
    method = door.route[0]  # type: ignore[index]
    url = _url(door, w)
    r = c.post(url, json={"formats": ["text"]}) if method == "POST" else c.get(url)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
    return r.status_code, body


def _ctx(state: Path, handle: str | None, surface: str) -> SkillContext:
    principal = PrincipalContext(handle=handle, posture="sso", assured=True) if handle else None
    return SkillContext(registry=bind_default(), state_dir=state, logger=logging.getLogger("ld1"),
                        principal=principal, actor=handle or "", surface=surface)  # fmt: skip


def drive_skill(door: g.Door, transport: str, w: dict, who: dict, tmp: Path):
    params = {"site": SITE, "logbook": LOG}
    if door.skill == "attest.export":
        params["out"] = str(tmp / f"pkg-{transport}")
    if door.skill == "attest.reference":  # a reference names its record (unit LD-3)
        params[door.record_param] = w["rec"]["attestation_id"]
    if door.skill == "attest.rehydrate":  # a rehydration names its archive (unit LD-8)
        params["archive_id"] = w.get("archive_id") or "no-such-archive"
    surface = {"agent_tool": "chat"}.get(transport, transport)
    c = _ctx(w["state"], who["handle"], surface)
    token = site_authority.bind_caller_site(who["bound"], principal=who["handle"]) if who["bound"] else None
    try:
        result = invoke_capability(c.registry, door.skill, params, c, surface=transport)
    finally:
        if token is not None:
            site_authority.unbind_caller_site(token)
    return result


def records_at(state: Path, door: g.Door) -> list[dict]:
    return [r for r in audit_trail.read_chain(audit_trail.audit_path(state))
            if (r.get("access") or {}).get("door") == door.id]  # fmt: skip


# -- LD-1-A2, A4 and A6: every reason × every door × every transport ------------------------


@pytest.mark.acceptance("LD-1-A2")
@pytest.mark.parametrize(("door", "transport", "reason"), ROWS)
def test_ld1_a2_every_reason_at_every_door_is_the_same_deny_with_no_content(
    door, transport, reason, world, restore_log, monkeypatch, tmp_path
):
    who = arrange(reason, monkeypatch, world["state"])
    status, code = g.WIRE[reason]
    if transport == g.HTTP:
        got_status, body = drive_http(door, world, who)
        assert (got_status, (body or {}).get("code")) == (status, code), (door.id, body)
        assert set(body) == {"detail", "code"}, body  # a reason and a sentence; nothing else
        text = json.dumps(body)
    else:
        result = drive_skill(door, transport, world, who, tmp_path)
        assert not result.ok and result.value == g.Decision(False, reason).body(), result
        text = json.dumps(result.value) + " ".join(result.errors)
    # The refusal may echo what the caller sent (a log's name, a record id), never what is in it.
    for leak in (*CONTENT, world["rec"]["digest"]):
        assert leak not in text


@pytest.mark.acceptance("LD-1-A4")
@pytest.mark.parametrize(("door", "transport", "reason"), ROWS)
def test_ld1_a4_every_deny_writes_one_read_record_naming_the_reason(
    door, transport, reason, world, restore_log, monkeypatch, tmp_path
):
    who = arrange(reason, monkeypatch, world["state"])
    before = len(records_at(world["state"], door))
    if transport == g.HTTP:
        drive_http(door, world, who)
    else:
        drive_skill(door, transport, world, who, tmp_path)
    recorded = records_at(world["state"], door)[before:]
    assert len(recorded) == 1, (door.id, recorded)
    rec = recorded[0]
    a = rec["access"]
    assert rec["outcome"] == "denied"
    assert (a["reason"], a["refused"], a["transport"]) == (reason.value, g.WIRE[reason][1], transport)
    assert (a["ids"], a["count"]) == ([], 0)
    assert set(a) <= set(g.ACCESS_FIELDS)
    if reason is Reason.SITE:
        assert a["labels"] is None  # nothing about a hidden site's log travels, not even labels


@pytest.mark.acceptance("LD-1-A4")
@pytest.mark.parametrize("door", [d for d in g.doors() if d.shape == "target"], ids=lambda d: d.id)
def test_ld1_a4_every_allow_writes_one_read_record_with_the_labels_and_no_content(
    door, world, monkeypatch, tmp_path
):
    who = arrange(None, monkeypatch, world["state"])
    for transport in door.transports:
        before = len(records_at(world["state"], door))
        if transport == g.HTTP:
            status, _ = drive_http(door, world, who)
            served = 200 <= status < 300
        else:
            served = drive_skill(door, transport, world, who, tmp_path).ok
        recorded = records_at(world["state"], door)[before:]
        if not served:
            continue  # admitted, then nothing to serve (a period with nothing to close): no read
        assert len(recorded) == 1, (door.id, transport, recorded)
        a = recorded[0]["access"]
        assert recorded[0]["outcome"] == "ok" and recorded[0]["principal"] == ME
        assert a["labels"] == {"classification": "internal", "export_controlled": False}
        assert a["door"] == door.id and a["transport"] == transport and a["reason"] is None
    blob = audit_trail.audit_path(world["state"]).read_text()
    for leak in CONTENT:
        assert leak not in blob, leak


@pytest.mark.acceptance("LD-1-A6")
def test_ld1_a6_with_guard_unreachable_every_door_denies_and_nothing_falls_open(
    world, monkeypatch, tmp_path
):
    who = arrange(Reason.POLICY_UNAVAILABLE, monkeypatch, world["state"])
    for door in g.doors():
        if door.shape == "listing":
            continue
        for transport in door.transports:
            if transport == g.HTTP:
                status, body = drive_http(door, world, who)
                assert (status, body["code"]) == (503, "policy_unavailable"), door.id
            else:
                result = drive_skill(door, transport, world, who, tmp_path)
                assert not result.ok and result.value["code"] == "policy_unavailable", door.id
    # The listings refuse too: search skips the log provider, the Timeline raises to its route.
    with pytest.raises(search_engine._Skip):
        search_engine.logbooks("round", search_engine.Scope(principal=ME, sites=frozenset({SITE})), 10)
    with pytest.raises(g.PolicyUnavailable):
        rights.visible(_row(), _timeline_caller(ME))
    # An unwritable GUARD receipt is an outage, not a permission.
    from axiom.extensions.builtins.authz import decide as decide_mod

    monkeypatch.setattr(site_authority, "decide_site_read", lambda *_a, **_k: decide_mod.Verdict.from_decision(
        decide_mod.Decision.DENY, decide_mod.RECEIPT_WRITE_FAILED, decide_mod.UNRECORDED_RECEIPT))  # fmt: skip
    decision = g.check("attest.show", log_doors.LogCaller(handle=ME, state_dir=world["state"]),
                       log_doors.target(SITE, LOG))  # fmt: skip
    assert decision.reason is Reason.POLICY_UNAVAILABLE


# -- the listings: a refused log is absent, and the never-counted reasons leave no count ------


def _row():
    return {"lane": "attestations", "site": SITE, "logbook": LOG, "actor": ME}


def _timeline_caller(handle, sites=frozenset({SITE}), state=None):
    lc = log_doors.LogCaller(handle=handle, sites=sites, state_dir=state)
    return rights.Caller(handle=handle or "", sites=sites, log_caller=lc)


@pytest.mark.acceptance("LD-1-A2")
@pytest.mark.parametrize("reason", list(Reason), ids=lambda r: r.value)
def test_ld1_a2_the_listing_doors_leave_a_refused_log_out_by_the_same_check(
    reason, world, restore_log, monkeypatch
):
    who = arrange(reason, monkeypatch, world["state"])
    handle = who["handle"]
    sites = frozenset({who["bound"]}) if who["bound"] else frozenset({SITE})
    scope = search_engine.Scope(principal=handle or "", sites=sites)
    if reason in (Reason.UNRESOLVED_CALLER, Reason.POLICY_UNAVAILABLE):
        with pytest.raises(search_engine._Skip):
            search_engine.logbooks("round", scope, 10)
    else:
        assert search_engine.logbooks("round", scope, 10) == []
    caller = _timeline_caller(handle, sites, world["state"])
    if reason is Reason.POLICY_UNAVAILABLE:
        with pytest.raises(g.PolicyUnavailable):
            rights.visible(_row(), caller)
        return
    seen, why = rights.visible(_row(), caller)
    assert not seen
    # J1's rule: a label above the caller is hidden and counted; anything they may not know of
    # is never counted.
    assert why == ("classification" if reason not in g.NEVER_COUNTED else "scope")


@pytest.mark.acceptance("LD-1-A2")
def test_ld1_a2_the_listing_doors_serve_a_readable_log(world, monkeypatch):
    hits = search_engine.logbooks("round", search_engine.Scope(principal=ME, sites=frozenset({SITE})), 10)
    assert [h.id for h in hits] == [world["rec"]["attestation_id"]]
    assert rights.visible(_row(), _timeline_caller(ME, state=world["state"])) == (True, None)
    searched = [r for r in audit_trail.read_chain(audit_trail.audit_path(world["state"]))
                if (r.get("access") or {}).get("door") == "search.logbooks"]  # fmt: skip
    assert searched and searched[-1]["access"]["labels"] == _book().labels


# -- the Timeline's listing doors over HTTP, the stream among them (LB-J3-A20) ---------------

#: Every listing door the Timeline serves by a route: generated from the registry, so a new one
#: (a second stream, a lane endpoint) is driven here by its registration alone.
TIMELINE_DOORS = tuple(d for d in LISTING_DOORS if d.serves == "timeline" and d.route)
TIMELINE_ROWS = [
    pytest.param(d, r, id=f"{d.id}-{r.value if r else 'allowed'}")
    for d in TIMELINE_DOORS
    for r in (None, *Reason)
]  # fmt: skip


@pytest.fixture
def timeline(world, journal_db, monkeypatch):  # noqa: F811 - the J1 fixture
    """The world's signed entry as an attestations row in the journal, and the journal's routes
    on the session the world's client signs in with."""
    import uuid
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.attest.tests.test_api import BASE
    from axiom.extensions.builtins.journal import api as journal_api
    from axiom.extensions.builtins.journal import writer
    from axiom.extensions.builtins.journal.rows import Row

    monkeypatch.setenv(journal_api.INGEST_ENV, "0")
    monkeypatch.setattr(journal_api, "_state_dir", lambda: world["state"])
    monkeypatch.setattr(rights, "_lane_decider", lambda caller, lane: True)
    rights.forget_decisions()
    writer.forget_partitions()
    row = writer.append(Row(lane="attestations", subject=f"attest.{LOG}.signed", source="test",
                            source_id=uuid.uuid4().hex, at=datetime.now(UTC), status="ok",
                            summary="Round check signed", actor=ME, site=SITE, logbook=LOG))  # fmt: skip
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    journal_api.register_routes(router)
    app.include_router(router)
    yield {**world, "journal": TestClient(app, base_url=BASE), "row": row}
    rights.forget_decisions()


def _timeline_url(door: g.Door, row: dict) -> str:
    _method, path = door.route  # type: ignore[misc]
    url = path + "?lanes=attestations"
    if path.endswith("/stream"):
        # A token, which every reader may send back (ADR-271): a raw number is a whole-site reader's.
        from axiom.extensions.builtins.journal import numbers

        url += f"&once=1&last_event_id={numbers.token(row['seq'] - 1)}"
    return url


@pytest.mark.acceptance("LD-1-A2")
@pytest.mark.acceptance("LB-J3-A20")
@pytest.mark.parametrize(("door", "reason"), TIMELINE_ROWS)
def test_ld1_a2_every_reason_at_every_timeline_door_over_http(
    door, reason, timeline, restore_log, monkeypatch
):
    who = arrange(reason, monkeypatch, timeline["state"])
    rights.forget_decisions()
    c = timeline["journal"]
    c.cookies.clear()
    if who["user"] is not None:
        as_(c, who["user"])
    r = c.get(_timeline_url(door, timeline["row"]))
    if reason is Reason.UNRESOLVED_CALLER:
        assert (r.status_code, r.json()["code"]) == (401, "no_session")
        return
    if reason is Reason.POLICY_UNAVAILABLE:
        # Nothing falls open, at subscribe time too (LB-J3-A18).
        refused = {"detail": g.DETAIL[reason], "code": g.WIRE[reason][1]}
        assert (r.status_code, r.json()) == (503, refused)
        return
    assert r.status_code == 200, r.text
    stream = door.route[1].endswith("/stream")  # type: ignore[index]
    if stream:
        frames = [f for f in r.text.split("\n\n") if f.strip()]
        events = [f.split("\n")[-2] if f.startswith("id:") else f.split("\n")[0] for f in frames]
        rows = [json.loads(f.split("data: ", 1)[1]) for f in frames if "event: row" in f]
        # A stream carries rows and its own liveness, never a count or a gap for what it hid.
        assert set(events) <= {"event: ready", "event: row"}, events
        assert '"hidden"' not in r.text
    else:
        rows = r.json()["rows"]
        counted = reason is not None and reason not in g.NEVER_COUNTED
        assert r.json()["hidden"] == ({"attestations": 1} if counted else {}), r.json()["hidden"]
    if reason is None:
        assert [x["seq"] for x in rows] == [timeline["row"]["seq"]]
        return
    assert rows == []
    for leak in CONTENT:
        assert leak not in r.text, (door.id, leak)


# -- LD-1-A5: a caller outside the site's scope learns nothing about it ----------------------


@pytest.mark.acceptance("LD-1-A5")
def test_ld1_a5_a_hidden_site_is_answered_like_a_site_that_does_not_exist(world, monkeypatch):
    c = world["client"]
    for door in HTTP_DOORS:
        answers = []
        for site in (SITE, "no-such-site"):
            as_(c, OUTSIDER)
            url = _url(door, world).replace(f"site={SITE}", f"site={site}")
            r = c.post(url, json={}) if door.route[0] == "POST" else c.get(url)
            answers.append((r.status_code, r.json()))
        assert answers[0] == answers[1], door.id  # no existence hint
        assert SITE not in json.dumps(answers[0]), door.id  # no deny that names it
    # No row and no count, in either listing.
    outside = search_engine.Scope(principal="@op9:site-b", sites=frozenset({"site-b"}))
    assert search_engine.logbooks("round", outside, 10) == []
    assert rights.visible(_row(), _timeline_caller("@op9:site-b", frozenset({"site-b"}))) == (False, "scope")
    assert Reason.SITE in g.NEVER_COUNTED


# -- LD-1-A3: the matrix is generated, and a door without its rows fails ---------------------


def _drivable(door: g.Door, app_routes: set[tuple[str, str]], skills: set[str]) -> list[str]:
    """What is missing for the matrix to reach this door. Empty when it is fully driven."""
    missing = []
    if door.shape == "listing":
        if door.surface not in LISTING_SURFACES:
            missing.append(f"no listing driver for surface {door.surface!r}")
        return missing
    if g.HTTP in door.transports and door.route not in app_routes:
        missing.append(f"route {door.route} is not served")
    if door.skill and door.skill not in skills:
        missing.append(f"skill {door.skill} is not registered")
    return missing


#: Listing surfaces this file drives (search and the Timeline, above).
LISTING_SURFACES = frozenset({"search", "timeline"})


def _app_routes() -> set[tuple[str, str]]:
    from axiom.extensions.builtins.attest import api as attest_api
    from axiom.extensions.builtins.journal import api as journal_api
    from axiom.extensions.builtins.search import api as search_api

    router = fastapi.APIRouter(prefix="/api/v1")
    attest_api.register_routes(router)
    search_api.register_routes(router)
    journal_api.os.environ.setdefault(journal_api.INGEST_ENV, "0")
    journal_api.register_routes(router)
    return {(m, r.path) for r in router.routes for m in getattr(r, "methods", ()) if m != "HEAD"}


@pytest.mark.acceptance("LD-1-A3")
def test_ld1_a3_the_matrix_is_generated_from_the_registry_and_the_reason_enum(monkeypatch):
    monkeypatch.setenv("AXIOM_JOURNAL_INGEST", "0")
    expected = {(d, t, r) for d in g.doors() if d.shape == "target" for t in d.transports for r in Reason}
    assert {tuple(p.values) for p in ROWS} == expected
    assert {d.id for d in g.doors() if d.skill} == set(DOOR_SKILLS)
    routes = _app_routes()
    skills = set(bind_default().list("attest"))
    for door in g.doors():
        assert _drivable(door, routes, skills) == [], door.id


@pytest.mark.acceptance("LD-1-A3")
def test_ld1_a3_a_door_registered_without_its_rows_fails(monkeypatch):
    """The negative control: a new door that nothing here can reach is named."""
    ghost = g.Door(id="attest.http.ghost", extension="attest", surface="api", serves="records",
                   route=("GET", "/api/v1/attest/{logbook}/ghost"))  # fmt: skip
    assert _drivable(ghost, _app_routes(), set()) == [f"route {ghost.route} is not served"]
    lonely = g.Door(id="attest.reports", extension="attest", surface="reports", serves="records",
                    route=("GET", "/api/v1/reports"), shape="listing")  # fmt: skip
    assert _drivable(lonely, set(), set())


# -- LD-1-A1: the registry lists every surface, and a router walk finds no stray door --------

#: The homes whose routes serve records or their projections; every route they register under
#: ``/api/v1`` is a door or is declared not to be one.
RECORD_HOMES = ("attest", "journal", "search")

#: Modules outside the log's own extension that call its record readers, and the door each
#: one feeds. A new caller fails until it is a door here.
READERS_OUTSIDE = {
    "src/axiom/attest/query.py": "the store's query library under attest.filters",
    "src/axiom/attest/verify.py": "the chain verifier under the verify doors",
    "src/axiom/extensions/builtins/journal/sources.py": "Timeline ingest; served by journal.attestations",
    "src/axiom/extensions/builtins/search/engine.py": "the search provider; door search.logbooks",
}

_READERS = ("record", "records", "records_where", "briefs", "brief", "thread", "unacknowledged",
            "pending_cosigns", "RecordFilters")  # fmt: skip


@pytest.mark.acceptance("LD-1-A1")
def test_ld1_a1_every_route_in_a_record_home_is_a_door_or_says_why_not(monkeypatch):
    monkeypatch.setenv("AXIOM_JOURNAL_INGEST", "0")
    stray = []
    for method, path in sorted(_app_routes()):
        if not path.startswith("/api/v1/"):
            continue
        if g.by_route(method, path) is None and (method, path) not in g.not_doors():
            stray.append((method, path))
    assert stray == [], f"routes that serve records without a door: {stray}"
    assert all(why.strip() for why in g.not_doors().values())


@pytest.mark.acceptance("LD-1-A1")
def test_ld1_a1_every_surface_has_doors_or_says_why_it_has_none():
    with_doors = {d.surface for d in g.doors()}
    # A skill door is reached from the CLI and from chat (as an agent tool) as well as MCP.
    with_doors |= {{"cli": "cli", "agent_tool": "chat", "mcp": "mcp"}[t] for d in g.doors()
                   for t in d.transports if t != g.HTTP}  # fmt: skip
    for surface in g.SURFACES:
        assert surface in with_doors or g.no_door_surfaces().get(surface), surface


@pytest.mark.acceptance("LD-1-A1")
def test_ld1_a1_nothing_outside_the_log_reads_its_records_except_through_a_door():
    root = Path(__file__).resolve().parents[2]
    found = set()
    for path in (root / "src" / "axiom").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if "/tests/" in rel or rel.startswith("src/axiom/extensions/builtins/attest/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _READERS and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("service", "filters", "query")):  # fmt: skip
                found.add(rel)
    assert found <= set(READERS_OUTSIDE), sorted(found - set(READERS_OUTSIDE))


# -- the registry takes a later unit's rows as data ------------------------------------------


@pytest.mark.acceptance("LD-1-A3")
def test_ld1_a3_a_later_unit_adds_a_parameter_as_a_row_not_a_second_check():
    door = g.get("attest.http.records")
    try:
        added = g.add_param(door.id, "field.example")
        assert "field.example" in added.params and added.route == door.route
        assert g.get(door.id).params[-1] == "field.example"
    finally:
        g._doors[door.id] = door  # noqa: SLF001 - put the registry back as it was
