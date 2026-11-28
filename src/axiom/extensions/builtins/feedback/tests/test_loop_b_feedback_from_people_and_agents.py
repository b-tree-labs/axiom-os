# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-B1 to B7: feedback from people and agents, grouped by cause, rolled back by default.

One test per behaviour. ``beta_editor`` is on for ana, bo, cy, di and ana-agent by a user
override and is declared by ``core_ext``, whose ``export`` capability fails; ed was never
exposed to it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.feedback import store
from axiom.extensions.builtins.feedback.tests.conftest import call, report
from axiom.infra import audit_trail

PREFS = "axiom.extensions.builtins.notifications.preferences.resolve_role"
AUTO_STOP = "AXIOM_FLAG_FEEDBACK_AUTO_STOP"


def _web(monkeypatch, *, roles=("viewer",), sub="ana"):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.feedback import api

    monkeypatch.setattr(api, "_claims", lambda request: {"sub": sub, "roles": list(roles), "site": "acct"}
                        if sub else {})
    router = APIRouter()
    api.register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _needs_you(person, fb, tmp_path):
    from axiom.extensions.builtins.notifications.needs_you import needs_you

    return needs_you(person, inbox_store=fb["inbox"], approvals_path=tmp_path / "none.json",
                     due_provider=lambda p, d: [])


def _killed(fb) -> dict | None:
    row = fb["targeting"].rollout("beta_editor", "development")
    return row if row and row["kill_switch"] else None


def _inbox(fb, who):
    from axiom.extensions.builtins.notifications.inbox import list_unread

    return [r.summary for r in list_unread(fb["inbox"], recipient=who)]


@pytest.mark.acceptance("LOOP-B1")
def test_one_action_with_no_form_reports_from_the_cli_mcp_and_the_web(fb, monkeypatch, capsys):
    from axiom.extensions.builtins.features.tests.conftest import mcp
    from axiom.extensions.builtins.feedback import cli
    from axiom.extensions.builtins.feedback.skills import bind

    assert cli.main(["bad", "the", "export", "hangs"]) == 0
    assert "thanks" in capsys.readouterr().out
    assert mcp("axiom_feedback__report", {"verdict": "good"}, fb["state"], bind=bind)["ok"]
    assert _web(monkeypatch, sub="").post("/api/v1/feedback", json={"verdict": "bad"}).status_code == 401
    r = _web(monkeypatch).post("/api/v1/feedback", json={"verdict": "bad", "screen": "#/editor"})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["screen"] == "#/editor" and r.json()["report"]["reporter_kind"] == "person"
    assert not report(fb["state"], verdict="meh").ok
    assert sum(g["bad"] + g["good"] for g in store.groups()) == 3


@pytest.mark.acceptance("LOOP-B2")
def test_the_server_captures_the_context_the_reporter_never_types(fb):
    assert call("core_ext.search", {}, fb["state"]).ok  # what ana was doing
    r = report(fb["state"], text="results look stale").value["report"]
    assert (r["surface"], r["release"], r["node"], r["environment"]) == ("mcp", "0.71.0", "node-a", "development")
    assert r["flags_on"]["beta_editor"] == "user"  # resolved for ana, with what decided it
    assert r["capability"] == "core_ext.search" and r["flag"] == "beta_editor"
    assert r["text"] == "results look stale" and r["outcome"] is None


@pytest.mark.acceptance("LOOP-B3")
def test_an_agent_reports_the_call_it_flags_and_nothing_else_is_reported(fb):
    assert not call("core_ext.export", {}, fb["state"], actor="@ana-agent").ok
    assert store.groups() == []  # a failure is remembered by the dispatch hook, never reported by itself
    r = report(fb["state"], actor="@ana-agent", capability="core_ext.export").value["report"]
    assert (r["outcome"], r["reporter_kind"], r["flag"]) == ("failed", "agent", "beta_editor")
    assert "timed out" in r["error"]
    assert call("core_ext.search", {}, fb["state"], actor="@ana-agent").ok
    r = report(fb["state"], actor="@ana-agent", capability="core_ext.search", outcome="unusable").value["report"]
    assert r["outcome"] == "unusable" and r["error"] is None


@pytest.mark.acceptance("LOOP-B4")
def test_reports_group_by_cause_counted_with_distinct_reporters(fb, monkeypatch):
    monkeypatch.setenv(AUTO_STOP, "off")
    for who in ("@ana", "@ana", "@bo"):
        report(fb["state"], actor=who, capability="core_ext.export")
    report(fb["state"], actor="@bo", verdict="good", capability="core_ext.export")
    report(fb["state"], actor="@ana", capability="core_ext.search")
    report(fb["state"], actor="@ed", capability="core_ext.search")  # never exposed: the release is the cause
    by = {(g["capability"], g["flag"]): g for g in store.groups()}
    assert len(by) == 3
    export = by[("core_ext.export", "beta_editor")]
    assert (export["bad"], export["good"], export["reporters"], export["release"]) == (3, 1, 2, "0.71.0")
    assert by[("core_ext.search", None)]["rollback"] == "roll_back_slot"
    # Nobody implicates a flag they were never exposed to.
    refused = report(fb["state"], actor="@ed", flag="beta_editor")
    assert not refused.ok and "not on for you" in refused.errors[0]


@pytest.mark.acceptance("LOOP-B5", "LOOP-B7")
def test_a_cause_is_one_ask_in_its_owners_needs_you_with_the_rollback_chosen(fb, monkeypatch, tmp_path):
    monkeypatch.setenv(AUTO_STOP, "off")
    monkeypatch.setattr(PREFS, lambda r: {"@site-admin": "@sam"}.get(r, r))  # no flag owner yet: the site admins
    for who in ("@ana", "@bo", "@cy"):
        report(fb["state"], actor=who, capability="core_ext.export")
    report(fb["state"], actor="@ed", capability="core_ext.search")
    items = _needs_you("@sam", fb, tmp_path)["items"]
    assert [i["kind"] for i in items] == ["feedback", "feedback"]  # one per cause; not its notification too
    flag = next(i for i in items if "beta_editor" in i["title"])
    assert flag["title"].startswith("3 bad reports from 3 reporters")
    assert flag["action"]["label"] == "Stop this flag"
    assert flag["choices"][0]["label"] == "Fix forward instead" and flag["choices"][0]["needs"] == ["reason"]
    assert next(i for i in items if i is not flag)["action"]["label"] == "Roll back the slot"
    assert _needs_you("@ana", fb, tmp_path)["count"] == 0  # only the owner sees the ask
    assert len(_inbox(fb, "@sam")) == 2  # one notification per ask, not per report


@pytest.mark.acceptance("LOOP-B5", "LOOP-B7")
def test_the_owner_stops_the_flag_and_fixing_forward_needs_a_reason(fb, monkeypatch, tmp_path):
    monkeypatch.setenv(AUTO_STOP, "off")
    monkeypatch.setattr(PREFS, lambda r: {"@site-admin": "@sam"}.get(r, r))
    gid = report(fb["state"], capability="core_ext.export").value["group"]["id"]
    assert not call("feedback.decide", {"group": gid, "choice": "stop_flag"}, fb["state"], actor="@ana").ok
    out = call("feedback.decide", {"group": gid, "choice": "fix_forward"}, fb["state"], actor="@sam")
    assert not out.ok and "reason" in out.errors[0]
    assert call("feedback.decide", {"group": gid, "choice": "stop_flag"}, fb["state"], actor="@sam").ok
    assert _killed(fb)["kill_reason"] == "owner: 1 report of the flag beta_editor at core_ext.export"
    assert _needs_you("@sam", fb, tmp_path)["count"] == 0
    gid = report(fb["state"], actor="@ed", capability="core_ext.search").value["group"]["id"]
    out = call("feedback.decide", {"group": gid, "choice": "fix_forward", "reason": "0.71.1 fixes it"},
               fb["state"], actor="@sam")
    assert out.ok and out.value["group"]["status"] == "fixing_forward"


@pytest.mark.acceptance("LOOP-B5", "LOOP-B7")
def test_three_distinct_reporters_within_the_hour_stop_the_flag_once_and_tell_the_owner(fb, monkeypatch):
    monkeypatch.setenv(AUTO_STOP, "off")
    for who in ("@ana", "@bo", "@cy"):
        report(fb["state"], actor=who, capability="core_ext.search")
    assert _killed(fb) is None  # the automatic stop is off until its flag is on
    monkeypatch.delenv(AUTO_STOP)
    for who in ("@ana", "@ana", "@bo"):
        assert report(fb["state"], actor=who, capability="core_ext.export").value["auto_stop"] is None
    out = report(fb["state"], actor="@cy", capability="core_ext.export").value
    reason = "auto: 4 reports of the flag beta_editor at core_ext.export"
    assert out["auto_stop"]["stopped"] and _killed(fb)["kill_reason"] == reason
    assert report(fb["state"], actor="@di", capability="core_ext.export").value["auto_stop"] is None
    stops = [r for r in audit_trail.read_chain(audit_trail.audit_path(fb["state"])) if r["skill"] == "flags.kill"]
    assert len(stops) == 1 and stops[0]["change"]["kind"] == "kill-switch"
    assert [s for s in _inbox(fb, "@site-admin") if s.startswith("RIVET stopped")] == [
        "RIVET stopped beta_editor: 4 bad reports from 3 reporters"]


@pytest.mark.acceptance("LOOP-B5", "LOOP-B7")
def test_a_severe_report_stops_the_flag_at_once_and_a_slot_is_only_offered(fb):
    out = report(fb["state"], capability="core_ext.export", severe=True).value
    assert out["auto_stop"]["stopped"] and _killed(fb)["kill_reason"].endswith("(marked severe)")
    out = report(fb["state"], actor="@ed", capability="core_ext.search", severe=True).value
    assert out["auto_stop"] is None
    assert (out["group"]["status"], out["group"]["rollback"]) == ("open", "roll_back_slot")


@pytest.mark.acceptance("LOOP-B6")
def test_a_flags_people_view_reads_its_grouped_reports_by_skill_and_http(fb, monkeypatch):
    monkeypatch.setenv(AUTO_STOP, "off")
    report(fb["state"], capability="core_ext.export", text="the export hangs")
    report(fb["state"], actor="@bo", verdict="good", capability="core_ext.export")
    value = call("feedback.reports", {"flag": "beta_editor"}, fb["state"]).value
    assert (value["bad"], value["good"]) == (1, 1)
    assert [r["text"] for r in value["groups"][0]["recent"]] == [None, "the export hangs"]
    assert _web(monkeypatch).get("/api/v1/feedback/flags/beta_editor").status_code == 403
    r = _web(monkeypatch, roles=("site-admin",)).get("/api/v1/feedback/flags/beta_editor")
    assert r.status_code == 200 and r.json()["groups"][0]["id"] == value["groups"][0]["id"]
