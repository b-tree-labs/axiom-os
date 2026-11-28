# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The web routes the feature-flag screens read and write (SW-A5, A13 to A19, A23, A25).

Every route is thin: it shapes a request into the same ``flags.*`` skills the command
line and MCP run, through ``invoke_capability`` on the web surface, so a browser is
gated and audited exactly like the other surfaces. Every write below is for a site
admin (``site-admin``; a grant still stored as ``operator`` counts, ADR-261); the narrow ``admin``
role (Access admin) is not one.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import approvals
from axiom.extensions.builtins.features.tests.conftest import run, web

ENV = "development"
ROLLOUT = {"rules": [{"value": "on"}], "reason": "launch the editor"}


def _admin(monkeypatch, sub="ana"):
    """A site admin's client. ``web`` pins one session for the whole app, so make the client
    again before each call whenever two people take turns."""
    return web(monkeypatch, sub=sub, roles=("site-admin",), site="acct")


def _preview(client, key="beta_editor", body=None):
    return client.post(f"/api/v1/flags/{key}/rollout/preview", json=body or ROLLOUT)


def _apply(client, plan_hash, key="beta_editor", body=None):
    return client.post(f"/api/v1/flags/{key}/rollout/apply", json={**(body or ROLLOUT), "plan_hash": plan_hash})


# ------------------------------------------------------------------ SW-A25: who is a site admin


@pytest.mark.acceptance("SW-A25")
def test_sw_a25_site_admin_is_true_only_for_site_admin_or_operator(modes, monkeypatch):
    """SW-A25: ``site_admin`` is true for site-admin and operator, false for the narrow admin role
    and everyone else; the admin routes refuse anyone who is not a site admin."""
    for roles, expected in ((("site-admin",), True), (("operator",), True), (("admin",), False),
                            (("viewer",), False), ((), False)):
        body = web(monkeypatch, sub="x", roles=roles).get("/api/v1/flags").json()
        assert body["site_admin"] is expected, roles
    for roles in (("admin",), ("viewer",), ()):
        client = web(monkeypatch, sub="x", roles=roles)
        for path in ("/api/v1/flags/admin", "/api/v1/flags/cohorts", "/api/v1/flags/changes"):
            r = client.get(path)
            assert r.status_code == 403, (roles, path, r.text)
            assert "only a site admin" in r.json()["detail"]
        r = client.post("/api/v1/flags/beta_editor/stop", json={"reason": "x"})
        assert r.status_code == 403
        assert _preview(client).status_code == 403
    for roles in (("site-admin",), ("operator",)):
        assert web(monkeypatch, sub="x", roles=roles).get("/api/v1/flags/admin").status_code == 200


@pytest.mark.acceptance("SW-A25")
def test_sw_a25_the_admin_view_composes_flags_rollouts_stage_plans_stops_and_changes(modes, monkeypatch):
    """SW-A25: ``GET /flags/admin`` is one row per flag with its rollout, stage plan, stop and
    overrides, and the changes that wait."""
    ts = modes["targeting"]
    ts.put_rollout("fast_search", ENV, rules=[{"value": "on", "percentage": 50}], reason="r", by="@t")
    assert run("flags.stage_plan_set", {"key": "beta_editor", "reason": "r", "plan": {
        "stages": [{"percentage": 10}, {"everyone": True}], "soak": "1h", "value": "on", "gates": [],
        "on_fail": "halt"}}, modes["state"]).ok
    body = _admin(monkeypatch).get("/api/v1/flags/admin").json()
    assert body["environment"] == ENV and body["changes"] == []
    assert body["me"] == "@ana:acct"  # the form a change's author and approvals[].by take
    rows = {r["key"]: r for r in body["flags"]}
    for field in ("key", "description", "owner", "protected", "value", "waiting_on", "rollout", "stage_plan",
                  "stop", "overrides", "kind", "default", "remove_by", "variants", "needs", "source", "staged"):
        assert field in rows["fast_search"], field
    assert rows["fast_search"]["source"] == "rollout" and rows["fast_search"]["staged"] is None
    assert sorted(rows["layout"]["variants"]) == ["compact", "ratio", "wide"]
    assert rows["checkout"]["needs"] == [{"flag": "beta_editor", "values": ["on"], "met": False}]
    assert rows["panel"]["staged"] == {"value": "on", "source": "default"}  # waits on the off widgets extension
    assert rows["fast_search"]["rollout"] == {"rules": [{"value": "on", "percentage": 50}], "bucket_by": None}
    assert rows["beta_editor"]["stage_plan"]["status"] == "running"
    assert rows["timeline"]["rollout"] is None and rows["timeline"]["stop"] is None
    cohorts = _admin(monkeypatch).get("/api/v1/flags/cohorts").json()
    assert {"cohorts", "rollouts", "stage_plans"} <= set(cohorts)


# ------------------------------------------------------------------ SW-A23, SW-A19, SW-A5: stop


@pytest.mark.acceptance("SW-A23")
def test_sw_a23_stop_needs_a_reason_and_keeps_its_public_message_apart(modes, monkeypatch):
    """SW-A23: a stop needs a reason; it takes an optional public message, stored apart from the reason."""
    client = _admin(monkeypatch)
    assert client.post("/api/v1/flags/beta_editor/stop", json={}).status_code == 422
    r = client.post("/api/v1/flags/beta_editor/stop", json={"reason": "  "})
    assert r.status_code == 400 and "reason" in r.json()["detail"]
    r = client.post("/api/v1/flags/beta_editor/stop",
                    json={"reason": "the index is corrupt", "message": "Search is paused while we fix it."})
    assert r.status_code == 200, r.text
    stop = r.json()["stop"]
    assert stop["reason"] == "the index is corrupt" and stop["message"] == "Search is paused while we fix it."
    row = modes["targeting"].rollout("beta_editor", ENV)
    assert row["kill_reason"] == "the index is corrupt"
    assert row["kill_message"] == "Search is paused while we fix it."
    r = client.post("/api/v1/flags/beta_editor/release", json={"reason": "fixed"})
    assert r.status_code == 200, r.text
    row = modes["targeting"].rollout("beta_editor", ENV)
    assert row["kill_switch"] is False and row["kill_message"] is None
    # The old route still answers, and takes a message too.
    r = client.post("/api/v1/flags/beta_editor/kill", json={"reason": "again", "message": "Paused."})
    assert r.status_code == 200 and modes["targeting"].rollout("beta_editor", ENV)["kill_message"] == "Paused."


@pytest.mark.acceptance("SW-A19")
def test_sw_a19_everyone_sees_the_public_message_never_the_reason(modes, monkeypatch):
    """SW-A19: ``GET /flags`` carries ``stopped`` with the public message only."""
    _admin(monkeypatch).post("/api/v1/flags/beta_editor/stop",
                             json={"reason": "secret internal cause", "message": "Back soon."})
    _admin(monkeypatch).post("/api/v1/flags/fast_search/stop", json={"reason": "another internal cause"})
    r = web(monkeypatch, sub="pat", roles=("viewer",)).get("/api/v1/flags")
    body = r.json()
    assert body["stopped"] == {"beta_editor": {"message": "Back soon."}, "fast_search": {"message": None}}
    assert "secret internal cause" not in r.text and "another internal cause" not in r.text


@pytest.mark.acceptance("SW-A5")
def test_sw_a5_a_stop_is_never_held_and_shows_once(modes, monkeypatch):
    """SW-A5: under a two-person policy a stop still applies at once, and the admin view shows one row."""
    approvals.set_environment_policies({ENV: {"two_person": True}})
    client = _admin(monkeypatch)
    r = client.post("/api/v1/flags/beta_editor/stop", json={"reason": "incident", "message": "Paused."})
    assert r.status_code == 200, r.text
    assert r.json()["flags"]["beta_editor"] == "off"
    body = client.get("/api/v1/flags/admin").json()
    rows = [f for f in body["flags"] if f["key"] == "beta_editor"]
    assert len(rows) == 1
    stop = rows[0]["stop"]
    assert stop["reason"] == "incident" and stop["message"] == "Paused." and stop["by"] and stop["at"]
    assert body["changes"] == []


# ------------------------------------------------------------------ SW-A14, SW-A15: Change, then Apply


@pytest.mark.acceptance("SW-A14")
def test_sw_a14_rollout_preview_returns_a_plan_and_who_it_affects(people, monkeypatch):
    """SW-A14: a preview writes nothing and returns the plan hash, the held action, the affected counts
    and how many approvals it needs; the reason is optional; it can name a time and a zone."""
    people["targeting"].put_cohort("pilots", principals=["ana", "bob"], by="@t")
    client = _admin(monkeypatch)
    assert _preview(client, body={"rules": [{"value": "on"}]}).status_code == 200  # no reason needed
    r = _preview(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_hash"] and body["held_action_id"] and "presentation" not in body
    assert set(body["affected"]) == {"count", "sample", "per_rule", "population"}
    assert body["affected"]["count"] == 2 and body["approvals_needed"] == 0
    assert body["preview"]["after"]["rules"] == [{"value": "on"}]
    assert people["targeting"].rollout("beta_editor", ENV) is None
    timed = _preview(client, body={**ROLLOUT, "at": "2099-01-01T09:00", "timezone": "America/Chicago"}).json()
    assert timed["preview"]["scheduled_for"]["timezone"] == "America/Chicago"
    assert timed["plan_hash"] != body["plan_hash"]
    approvals.set_environment_policies({ENV: {"two_person": True}})
    assert _preview(client).json()["approvals_needed"] == 1


@pytest.mark.acceptance("SW-A15")
def test_sw_a15_a_site_admin_applies_what_they_previewed(people, monkeypatch):
    """SW-A15: Apply runs exactly what was previewed: a hash that is not the call's is 409, a spent
    plan is 409, and nothing is signed."""
    ts = people["targeting"]
    preview = _preview(_admin(monkeypatch)).json()
    assert _apply(_admin(monkeypatch), "0" * 32).status_code == 409
    other = {**ROLLOUT, "reason": "another plan"}
    assert _apply(_admin(monkeypatch), preview["plan_hash"], body=other).status_code == 409
    assert ts.rollout("beta_editor", ENV) is None
    r = _apply(_admin(monkeypatch), preview["plan_hash"])
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert ts.rollout("beta_editor", ENV)["rules"] == [{"value": "on"}]
    assert _apply(_admin(monkeypatch), preview["plan_hash"]).status_code == 409


@pytest.mark.acceptance("SW-A15")
def test_sw_a15_only_a_site_admin_applies(people, monkeypatch):
    """SW-A15: the Access admin and a creator are refused, whatever they hold."""
    preview = _preview(_admin(monkeypatch)).json()
    for roles in (("admin",), ("creator",)):
        r = _apply(web(monkeypatch, sub="x", roles=roles, site="acct"), preview["plan_hash"])
        assert r.status_code == 403 and "only a site admin" in r.json()["detail"]
    assert people["targeting"].rollout("beta_editor", ENV) is None


@pytest.mark.acceptance("SW-A15")
def test_sw_a15_the_audit_record_says_which_surface_a_change_came_through(people, monkeypatch):
    """SW-A15: every change is audited with who, what, before and after, and via (web, chat, mcp, cli)."""
    from axiom.infra import audit_trail

    _apply(_admin(monkeypatch), _preview(_admin(monkeypatch)).json()["plan_hash"])
    run("flags.cohort_set", {"key": "pilots", "principals": ["ana"]}, people["state"], actor="@ana:acct")
    records = audit_trail.read_chain(audit_trail.audit_path(people["state"]))
    changes = [r["change"] for r in records if r.get("change")]
    rollout = next(c for c in changes if c["kind"] == "rollout")
    assert rollout["via"] == "web" and rollout["new"] == {"rules": [{"value": "on"}], "bucket_by": None}
    assert next(c for c in changes if c["kind"] == "cohort")["via"] == "cli"
    assert any(r.get("principal") == "@ana:acct" and r.get("skill") == "flags.rollout_set" for r in records)


@pytest.mark.acceptance("SW-A14", "SW-A15")
def test_sw_a14_a15_a_cohort_is_previewed_and_applied_the_same_way(people, monkeypatch):
    """SW-A14, SW-A15: cohorts follow the same preview and apply."""
    body = {"principals": ["ana", "bob"], "name": "Pilots"}
    r = _admin(monkeypatch).post("/api/v1/flags/cohorts/pilots/preview", json=body)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["plan_hash"] and preview["affected"]["count"] == 2
    assert people["targeting"].cohort("pilots") is None
    r = _admin(monkeypatch).post("/api/v1/flags/cohorts/pilots/apply", json={**body, "plan_hash": preview["plan_hash"]})
    assert r.status_code == 200, r.text
    assert people["targeting"].cohort("pilots")["principals"] == ["ana", "bob"]


@pytest.mark.acceptance("SW-A11")
def test_sw_a11_a_new_cohort_is_created_through_preview_and_apply(people, monkeypatch):
    """SW-A11: a key nobody has used creates a cohort with its name, description, rule and lists."""
    body = {"name": "Newcomers", "description": "people in their first week",
            "rules": {"attr": "locale", "op": "eq", "value": "en-US"},
            "principals": ["dee"], "accounts": ["acme"], "reason": "a new cohort"}
    preview = _admin(monkeypatch).post("/api/v1/flags/cohorts/newcomers/preview", json=body).json()
    assert preview["preview"]["before"] is None
    r = _admin(monkeypatch).post("/api/v1/flags/cohorts/newcomers/apply",
                                 json={**body, "plan_hash": preview["plan_hash"]})
    assert r.status_code == 200, r.text
    made = people["targeting"].cohort("newcomers")
    assert (made["name"], made["description"], made["principals"], made["accounts"]) == (
        "Newcomers", "people in their first week", ["dee"], ["acme"])
    assert made["rules"] == {"attr": "locale", "op": "eq", "value": "en-US"}


# ------------------------------------------------------------------ SW-A13: import


@pytest.mark.acceptance("SW-A13")
def test_sw_a13_import_preview_counts_joins_leaves_and_unknown_rows(people, monkeypatch):
    """SW-A13: an import preview says who joins, who leaves and which rows this node does not know,
    opens a plan, and changes nothing until it is applied."""
    ts = people["targeting"]
    ts.put_cohort("pilots", principals=["ana", "cy"], by="@t")
    body = {"file_text": "principal\nana\nbob\nzed\n", "reason": "the spring list"}
    r = _admin(monkeypatch).post("/api/v1/flags/cohorts/pilots/import/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["joins"] == 2 and out["leaves"] == 1
    assert out["unknown"]["count"] == 2 and set(out["unknown"]["sample"]) == {"bob", "zed"}
    assert out["plan_hash"] and ts.cohort("pilots")["principals"] == ["ana", "cy"]
    r = _admin(monkeypatch).post("/api/v1/flags/cohorts/pilots/import/apply",
                                 json={**body, "plan_hash": out["plan_hash"]})
    assert r.status_code == 200, r.text
    assert ts.cohort("pilots")["principals"] == ["ana", "bob", "zed"]


# ------------------------------------------------------------------ SW-A16, SW-A17: two people


def _held(monkeypatch, body=None):
    """Ana previews and applies under a two-person rule; the 202 answer."""
    preview = _preview(_admin(monkeypatch, "ana"), body=body).json()
    r = _apply(_admin(monkeypatch, "ana"), preview["plan_hash"], body=body)
    assert r.status_code == 202, r.text
    return r.json()


def _approve(monkeypatch, change_id, sub, roles=("site-admin",)):
    return web(monkeypatch, sub=sub, roles=roles, site="acct").post(
        f"/api/v1/flags/changes/{change_id}/approve", json={"reason": "looked at it"})


def _needs(people, person):
    from axiom.extensions.builtins.notifications.needs_you import needs_you

    return [i for i in needs_you(person, inbox_store=people["inbox"], due_provider=lambda p, d: [],
                                 approvals_path=people["state"] / "none.json")["items"]
            if (i.get("href") or "").startswith("#/settings/flags/change/")]


@pytest.mark.acceptance("SW-A16")
def test_sw_a16_a_two_person_change_waits_and_its_author_can_withdraw_but_not_approve(people, monkeypatch):
    """SW-A16: under a two-person rule Apply waits (202) and nothing changes; the author cannot approve
    their own change, and may withdraw it; somebody who is not a site admin may not."""
    approvals.set_environment_policies({ENV: {"two_person": True}})
    body = _held(monkeypatch)
    change = body["pending"]["change"]
    assert body["applied"] is False and change["id"] and change["author"] == "@ana:acct"
    assert change["approvals"] == [] and change["policy"] and body["waiting"].endswith(".")
    assert people["targeting"].rollout("beta_editor", ENV) is None
    assert [c["id"] for c in _admin(monkeypatch).get("/api/v1/flags/changes").json()["changes"]] == [change["id"]]

    own = _approve(monkeypatch, change["id"], "ana")
    assert own.status_code == 403 and "author" in own.json()["detail"]
    stranger = web(monkeypatch, sub="pat", roles=("viewer",))
    assert stranger.post(f"/api/v1/flags/changes/{change['id']}/withdraw", json={"reason": "no"}).status_code == 403
    r = _admin(monkeypatch, "ana").post(f"/api/v1/flags/changes/{change['id']}/withdraw", json={"reason": "not yet"})
    assert r.status_code == 200, r.text
    assert r.json()["change"]["status"] == "cancelled"
    assert people["targeting"].rollout("beta_editor", ENV) is None


@pytest.mark.acceptance("SW-A16")
def test_sw_a16_a_waiting_change_asks_the_other_site_admins_in_needs_you(people, monkeypatch):
    """SW-A16 (ADR-253): a change that waits is an ask in Needs you for every other site admin, never the
    author, linking to the change; withdrawing it resolves the ask."""
    approvals.set_environment_policies({ENV: {"two_person": True}})
    change = _held(monkeypatch)["pending"]["change"]
    (item,) = _needs(people, "@bob:acct")
    assert item["href"] == f"#/settings/flags/change/{change['id']}"
    assert item["title"] == "@ana:acct asks you to approve a change to beta_editor (development)."
    assert _needs(people, "@ana:acct") == [] and _needs(people, "@pat:acct") == []
    _admin(monkeypatch, "ana").post(f"/api/v1/flags/changes/{change['id']}/withdraw", json={"reason": "no"})
    assert _needs(people, "@bob:acct") == []


@pytest.mark.acceptance("SW-A17")
def test_sw_a17_a_second_site_admin_approves_and_it_applies(people, monkeypatch):
    """SW-A17: a second site admin approves; that completes the approvals, the change applies and the ask
    is resolved. The Access admin and a creator cannot approve; /cosign still answers as an alias."""
    approvals.set_environment_policies({ENV: {"two_person": True}})
    change = _held(monkeypatch)["pending"]["change"]
    for roles in (("admin",), ("creator",)):
        assert _approve(monkeypatch, change["id"], "x", roles=roles).status_code == 403
    r = _approve(monkeypatch, change["id"], "bob")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True and body["change"]["status"] == "applied"
    assert [a["by"] for a in body["change"]["approvals"]] == ["@bob:acct"]
    assert people["targeting"].rollout("beta_editor", ENV)["rules"] == [{"value": "on"}]
    assert _needs(people, "@bob:acct") == []
    again = _admin(monkeypatch, "bob").post(f"/api/v1/flags/changes/{change['id']}/cosign", json={})
    assert again.status_code in (400, 409)


@pytest.mark.acceptance("SW-A17")
def test_sw_a17_an_approved_scheduled_change_waits_for_its_time(people, monkeypatch):
    """SW-A17: an approved change that names a time is scheduled, not applied now."""
    approvals.set_environment_policies({ENV: {"two_person": True}})
    body = {**ROLLOUT, "at": "2099-01-01T09:00", "timezone": "America/Chicago"}
    change = _held(monkeypatch, body=body)["pending"]["change"]
    r = _approve(monkeypatch, change["id"], "bob")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is False and r.json()["change"]["status"] == "scheduled"
    assert people["targeting"].rollout("beta_editor", ENV) is None


# ------------------------------------------------------------------ Q16: waiting for approval


@pytest.mark.acceptance("Q16")
def test_q16_a_change_waiting_for_approval_is_visible_to_the_other_site_admins(people, monkeypatch):
    """Q16: once a change waits for a second person, every site admin sees it on the cohort or flag page;
    a second person's preview of the same subject names it; its author may withdraw it."""
    approvals.set_environment_policies({ENV: {"two_person": True}})
    assert _admin(monkeypatch, "bob").get("/api/v1/flags/waiting?subject=flag:beta_editor").json() == {
        "waiting": []}
    _preview(_admin(monkeypatch, "ana"))  # a preview alone is not waiting for anybody
    assert _admin(monkeypatch, "bob").get("/api/v1/flags/waiting?subject=flag:beta_editor").json()["waiting"] == []
    change = _held(monkeypatch)["pending"]["change"]
    (row,) = _admin(monkeypatch, "bob").get("/api/v1/flags/waiting?subject=flag:beta_editor").json()["waiting"]
    assert row == {"kind": "flag", "key": "beta_editor", "author": "@ana:acct", "author_name": "@ana:acct",
                   "checked_at": change["created_at"], "change_id": change["id"]}
    assert _admin(monkeypatch, "bob").get("/api/v1/flags/waiting?subject=cohort:pilots").json()["waiting"] == []
    second = _preview(_admin(monkeypatch, "bob"), body={**ROLLOUT, "reason": "mine instead"}).json()
    assert second["waiting"]["author"] == "@ana:acct" and second["waiting"]["change_id"] == change["id"]
    assert web(monkeypatch, sub="pat", roles=("viewer",)).get("/api/v1/flags/waiting").status_code == 403
    assert _admin(monkeypatch, "bob").post(f"/api/v1/flags/waiting/{change['id']}/withdraw", json={}).status_code == 403
    r = _admin(monkeypatch, "ana").post(f"/api/v1/flags/waiting/{change['id']}/withdraw", json={})
    assert r.status_code == 200 and r.json()["change"]["status"] == "cancelled"
    assert _admin(monkeypatch, "bob").get("/api/v1/flags/waiting").json()["waiting"] == []


@pytest.mark.acceptance("Q16")
def test_q16_a_draft_is_its_owner_s_alone(people, monkeypatch):
    """Q16: another site admin gets 404 for my draft on every verb, never 403."""
    _admin(monkeypatch, "ana").put("/api/v1/flags/drafts/cohorts/pilots", json={"body": {"principals": ["dee"]}})
    bob = _admin(monkeypatch, "bob")
    assert bob.get("/api/v1/flags/drafts/cohorts/pilots").status_code == 404
    assert bob.delete("/api/v1/flags/drafts/cohorts/pilots").status_code == 404
    out = run("flags.cohort_draft_get", {"key": "pilots"}, people["state"], actor="@bob:acct", surface="mcp")
    assert not out.ok and "no cohort draft" in out.errors[0]
    assert _admin(monkeypatch, "ana").get("/api/v1/flags/drafts/cohorts/pilots").status_code == 200


# ------------------------------------------------------------------ stages

PLAN = {"stages": [{"percentage": 10}, {"everyone": True}], "soak": "1h", "value": "on", "gates": [],
        "on_fail": "halt"}


def test_a_stage_plan_is_edited_through_preview_and_apply(people, monkeypatch):
    """Stages "Edit plan": the same preview and apply as a rollout; 202 under two people."""
    body = {"plan": PLAN}
    r = _admin(monkeypatch).post("/api/v1/flags/beta_editor/stages/preview", json=body)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["plan_hash"] and preview["held_action_id"]
    assert len(preview["preview"]["stages"]) == 2 and preview["approvals_needed"] == 0
    assert people["targeting"].stage_plan("beta_editor", ENV) is None
    r = _admin(monkeypatch).post("/api/v1/flags/beta_editor/stages/apply",
                                 json={**body, "plan_hash": preview["plan_hash"]})
    assert r.status_code == 200, r.text
    assert people["targeting"].stage_plan("beta_editor", ENV)["status"] == "running"

    approvals.set_environment_policies({ENV: {"two_person": True}})
    body = {"plan": {**PLAN, "stages": [{"percentage": 20}]}, "reason": "twenty"}
    preview = _admin(monkeypatch).post("/api/v1/flags/beta_editor/stages/preview", json=body).json()
    r = _admin(monkeypatch).post("/api/v1/flags/beta_editor/stages/apply",
                                 json={**body, "plan_hash": preview["plan_hash"]})
    assert r.status_code == 202 and r.json()["pending"]["change"]["id"]


@pytest.mark.acceptance("SW-A18")
def test_sw_a18_who_to_ask_comes_from_the_directory(people, monkeypatch):
    """SW-A18: the site admin to ask is read from the directory (people holding site-admin), the first
    in a stable order; the settings stay the fallback."""
    out = web(monkeypatch, sub="pat", roles=("viewer",)).get("/api/v1/flags/beta_editor/explain").json()
    assert out["ask"] == {"principal": "@ana:acct", "name": "@ana:acct"}


# ------------------------------------------------------------------ node policies


def test_the_node_policy_rows_carry_their_choices_and_are_set_over_the_web(modes, monkeypatch):
    """Maintenance and the record of truth are rows with a label and their choices; a site admin sets
    them with PATCH /features/{key} {value}, through features.maintenance and features.record_of_truth."""
    from axiom.infra import node_functions as nf

    client = _admin(monkeypatch)
    rows = {r["name"]: r for r in client.get("/api/v1/features").json()["switches"]}
    for name, values in (("maintenance", {"off", "requests", "sessions"}), ("record-of-truth", {"upstream", "local"}),
                         ("agents", {"none", "assist", "local"})):
        details = rows[name]["details"]
        assert details["label"] and {c["value"] for c in details["choices"]} == values
        assert all(c["label"] for c in details["choices"])
    r = client.patch("/api/v1/features/maintenance", json={"value": "requests"})
    assert r.status_code == 200, r.text
    assert nf.load().maintenance_level == "requests"
    r = client.patch("/api/v1/features/record-of-truth", json={"value": "local"})
    assert r.status_code == 200, r.text
    assert nf.load().record_of_truth_in_force == "local"
    assert client.patch("/api/v1/features/maintenance", json={"value": "always"}).status_code == 400
    viewer = web(monkeypatch, sub="pat", roles=("viewer",))
    assert viewer.patch("/api/v1/features/maintenance", json={"value": "off"}).status_code == 403



def test_a_site_admin_halts_a_stage_plan_at_once(modes, monkeypatch):
    """Halting a staged rollout applies at once, like a stop."""
    assert run("flags.stage_plan_set", {"key": "beta_editor", "reason": "r", "plan": {
        "stages": [{"percentage": 10}], "soak": "1h", "value": "on", "gates": [], "on_fail": "halt"}},
        modes["state"]).ok
    client = _admin(monkeypatch)
    assert client.post("/api/v1/flags/beta_editor/stages/halt", json={"reason": ""}).status_code == 400
    r = client.post("/api/v1/flags/beta_editor/stages/halt", json={"reason": "errors went up"})
    assert r.status_code == 200, r.text
    assert modes["targeting"].stage_plan("beta_editor", ENV)["status"] == "halted"


# ------------------------------------------------------------------ SW-A18: explain, scoped to the viewer


def _stage(modes, stages):
    assert run("flags.stage_plan_set", {"key": "beta_editor", "reason": "r", "plan": {
        "stages": stages, "soak": "1h", "value": "on", "gates": [], "on_fail": "halt"}}, modes["state"]).ok


@pytest.mark.acceptance("SW-A18")
def test_sw_a18_a_person_gets_one_sentence_and_who_to_ask(modes, monkeypatch):
    """SW-A18: a person gets the value, one plain sentence and the site admin to ask; never the trace."""
    def person():
        return web(monkeypatch, sub="pat", roles=("viewer",))

    r = person().get("/api/v1/flags/beta_editor/explain")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"flag", "value", "sentence", "ask"}
    assert body["sentence"] == "This is not turned on here." and body["ask"] is None

    modes["targeting"].put_cohort("pilots", principals=["pat"], by="@t")
    _stage(modes, [{"cohort": "pilots"}, {"everyone": True}])
    assert person().get("/api/v1/flags/beta_editor/explain").json()["sentence"] == (
        "This is being turned on in stages, and you're in the current one.")
    other = web(monkeypatch, sub="quin", roles=("viewer",))
    assert other.get("/api/v1/flags/beta_editor/explain").json()["sentence"] == (
        "This is being turned on in stages; you're not in the current stage.")

    _admin(monkeypatch).post("/api/v1/flags/beta_editor/stop", json={"reason": "incident"})
    assert person().get("/api/v1/flags/beta_editor/explain").json()["sentence"] == (
        "This has been paused by your site's admins.")
    modes["targeting"].put_rollout("fast_search", ENV, rules=[{"value": "on"}], reason="r", by="@t")
    assert person().get("/api/v1/flags/fast_search/explain").json()["sentence"] == "This is on for everyone here."

    from axiom.extensions.builtins.features.skills import _loop

    monkeypatch.setattr(_loop, "_setting", lambda key: {
        "features.site_admin.principal": "@ana:acct", "features.site_admin.name": "Ana"}.get(key))
    assert person().get("/api/v1/flags/timeline/explain").json()["ask"] == {"principal": "@ana:acct", "name": "Ana"}
    assert person().get("/api/v1/flags/beta_editor/explain?user=ana").status_code == 403
    assert web(monkeypatch, sub="z", roles=("admin",)).get(
        "/api/v1/flags/beta_editor/explain?user=ana").status_code == 403


@pytest.mark.acceptance("SW-A18")
def test_sw_a18_a_site_admin_also_gets_the_trace(modes, monkeypatch):
    """SW-A18: a site admin gets the same sentence plus the full trace, for anyone."""
    modes["targeting"].put_rollout("fast_search", ENV, rules=[{"value": "on"}], reason="r", by="@t")
    client = _admin(monkeypatch)
    body = client.get("/api/v1/flags/fast_search/explain").json()
    assert body["sentence"] == "This is on for everyone here."
    assert body["trace"]["decided_by"] == "rollout" and body["trace"]["detail"]["rule_index"] == 0
    other = client.get("/api/v1/flags/fast_search/explain?user=pat").json()
    assert other["trace"]["subject"]["principal"] == "pat"


@pytest.mark.acceptance("SW-A18")
def test_sw_a18_a_variant_says_which_version_you_have(modes, monkeypatch):
    """SW-A18: a split names the version this person has, and that they keep it."""
    modes["targeting"].put_rollout("layout", ENV, rules=[{"split": [{"variant": "compact", "weight": 50},
                                                                    {"variant": "wide", "weight": 50}]}],
                                   reason="r", by="@t")
    body = web(monkeypatch, sub="pat").get("/api/v1/flags/layout/explain").json()
    assert body["sentence"] == f"We're trying two versions; you have {body['value']}, and you'll keep it."
