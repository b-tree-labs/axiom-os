# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dual drive: the cohort rule builder and chat edit ONE draft (owner decision, round 4).

A draft is a person's unsigned work on a cohort or a rollout, stored once per (person, site,
kind, key) in the flags schema. The web builder (HTTP), chat and MCP clients (MCP) and the
command line edit the same draft through the same skills, and each save says where it came
from. A draft never applies, never signs and never opens a plan: saving is the existing
preview, then a person signs (DC-10). Also here: the builder's live count (SW-A29), its
Check step (SW-A30) and its attribute vocabulary.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.features.tests.conftest import mcp, run, web

COHORT = {"name": "Pilots", "description": "the first users",
          "rules": {"attr": "locale", "op": "eq", "value": "en-US"}, "principals": ["dee"], "accounts": []}
ROLLOUT = {"rules": [{"value": "on", "percentage": 50}], "bucket_by": None, "environment": "development"}
DRAFT_SKILLS = tuple(f"flags.{kind}_draft_{verb}" for kind in ("cohort", "rollout")
                     for verb in ("create", "update", "get", "discard"))


def _admin(monkeypatch, sub="ana"):
    return web(monkeypatch, sub=sub, roles=("site-admin",), site="acct")


def _held(state) -> list:
    path = state / "orchestrator" / "approvals.json"
    if not path.exists():
        return []
    from axiom.infra.orchestrator.approval_store import FileActionStore

    return FileActionStore(path).all()


# ------------------------------------------------------------------ the one draft, over HTTP


def test_dual_a_cohort_draft_is_saved_read_and_discarded_over_the_web(modes, monkeypatch):
    """DUAL-1: PUT creates and updates the session's own draft with a revision; GET reads it (304
    when nothing changed since a revision); a stale revision is 409 with the current draft; a rule
    the grammar refuses is 400 in words; DELETE discards it."""
    client = _admin(monkeypatch)
    r = client.put("/api/v1/flags/drafts/cohorts/pilots", json={"body": COHORT})
    assert r.status_code == 200, r.text
    draft = r.json()["draft"]
    assert (draft["kind"], draft["key"], draft["revision"]) == ("cohort", "pilots", 1)
    assert draft["body"] == COHORT and draft["updated_by"] == "@ana:acct" and draft["updated_via"] == "web"
    assert draft["created_at"] and draft["updated_at"]
    assert client.get("/api/v1/flags/drafts/cohorts/pilots").json()["draft"]["revision"] == 1
    assert client.get("/api/v1/flags/drafts/cohorts/pilots?since=1").status_code == 304

    second = {**COHORT, "principals": ["dee", "eve"]}
    r = client.put("/api/v1/flags/drafts/cohorts/pilots", json={"body": second, "revision": 1})
    assert r.status_code == 200 and r.json()["draft"]["revision"] == 2
    stale = client.put("/api/v1/flags/drafts/cohorts/pilots", json={"body": COHORT, "revision": 1})
    assert stale.status_code == 409 and stale.json()["draft"]["revision"] == 2
    assert stale.json()["draft"]["body"]["principals"] == ["dee", "eve"]

    bad = client.put("/api/v1/flags/drafts/cohorts/pilots",
                     json={"body": {**COHORT, "rules": {"attr": "locale", "op": "like", "value": "en"}}})
    assert bad.status_code == 400 and "Unknown operator 'like'" in bad.json()["detail"]

    assert client.delete("/api/v1/flags/drafts/cohorts/pilots").status_code == 200
    assert client.get("/api/v1/flags/drafts/cohorts/pilots").status_code == 404


def test_dual_a_draft_is_the_session_s_own_and_for_site_admins(modes, monkeypatch):
    """DUAL-2: another site admin does not see my draft; a person who is not a site admin has none."""
    _admin(monkeypatch).put("/api/v1/flags/drafts/cohorts/pilots", json={"body": COHORT})
    assert _admin(monkeypatch, "bob").get("/api/v1/flags/drafts/cohorts/pilots").status_code == 404
    viewer = web(monkeypatch, sub="pat", roles=("viewer",))
    assert viewer.get("/api/v1/flags/drafts/cohorts/pilots").status_code == 403
    assert viewer.put("/api/v1/flags/drafts/cohorts/pilots", json={"body": COHORT}).status_code == 403


def test_dual_a_rollout_draft_follows_the_same_pattern(modes, monkeypatch):
    """DUAL-3: a rollout draft is validated against the rollout grammar and the declared flag."""
    client = _admin(monkeypatch)
    r = client.put("/api/v1/flags/drafts/rollouts/beta_editor", json={"body": ROLLOUT})
    assert r.status_code == 200, r.text
    draft = r.json()["draft"]
    assert draft["kind"] == "rollout" and draft["body"] == ROLLOUT and draft["revision"] == 1
    assert client.get("/api/v1/flags/drafts/rollouts/beta_editor?since=0").status_code == 200
    bad = client.put("/api/v1/flags/drafts/rollouts/beta_editor",
                     json={"body": {**ROLLOUT, "rules": [{"value": "on", "percentage": 140}]}})
    assert bad.status_code == 400 and "percentage" in bad.json()["detail"]
    assert client.put("/api/v1/flags/drafts/rollouts/nope", json={"body": ROLLOUT}).status_code == 404
    assert client.delete("/api/v1/flags/drafts/rollouts/beta_editor").status_code == 200


# ------------------------------------------------------------------ one object, every surface


def test_dual_chat_and_the_builder_edit_the_same_draft(modes, monkeypatch):
    """DUAL-4: a save from chat or MCP lands on the draft the builder shows, and says where it came
    from; the builder's next save sees the new revision."""
    client = _admin(monkeypatch)
    client.put("/api/v1/flags/drafts/cohorts/pilots", json={"body": COHORT})
    out = run("flags.cohort_draft_update", {"key": "pilots", "body": {**COHORT, "principals": ["dee", "fay"]},
                                            "revision": 1}, modes["state"], actor="@ana:acct", surface="mcp")
    assert out.ok, out.errors
    seen = client.get("/api/v1/flags/drafts/cohorts/pilots?since=1").json()["draft"]
    assert seen["revision"] == 2 and seen["updated_via"] == "mcp" and seen["body"]["principals"] == ["dee", "fay"]
    out = run("flags.cohort_draft_update", {"key": "pilots", "body": COHORT, "revision": 2}, modes["state"],
              actor="@ana:acct", surface="chat")
    assert out.ok and out.value["draft"]["updated_via"] == "chat"


def test_dual_the_command_line_edits_drafts_too(modes, capsys):
    """DUAL-5: ``flags draft cohort set|show|discard`` and ``flags draft rollout ...``."""
    from axiom.extensions.builtins.flags import cli

    assert cli.main(["draft", "cohort", "set", "pilots", "--body", json.dumps(COHORT)]) == 0
    capsys.readouterr()
    assert cli.main(["--json", "draft", "cohort", "show", "pilots"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["draft"]["body"] == COHORT and shown["draft"]["updated_via"] == "cli"
    assert cli.main(["draft", "rollout", "set", "beta_editor", "--body", json.dumps(ROLLOUT)]) == 0
    assert cli.main(["draft", "cohort", "discard", "pilots"]) == 0
    assert cli.main(["draft", "cohort", "show", "pilots"]) != 0


def test_dual_the_mcp_tools_say_what_a_draft_is_and_carry_the_rule_grammar(modes):
    """DUAL-6: each draft tool is on MCP with a description a model can act on, and the input schema
    of a write states the rule grammar exactly: the combinators, the leaf operators, the depth
    limit and the attribute names."""
    from axiom.extensions.builtins.features import attributes
    from axiom.extensions.builtins.features.skills import bind
    from axiom.extensions.builtins.features.targeting import LEAF_OPS, MAX_RULE_DEPTH
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    tools = {t.name: t for t in skill_tool_contribution(reg, ctx_factory=lambda: None).tools}
    for name in DRAFT_SKILLS:
        tool = tools["axiom_" + name.replace(".", "__")]
        text = tool.description.lower()
        assert "draft" in text and "never applies" in text and "apply it there" in text
    schema = tools["axiom_flags__cohort_draft_update"].input_schema
    rule = schema["$defs"]["rule"]
    leaf = next(s for s in rule["anyOf"] if "attr" in s.get("properties", {}))
    assert set(leaf["properties"]["op"]["enum"]) == set(LEAF_OPS)
    assert set(leaf["properties"]["attr"]["enum"]) == {n for n, a in attributes.catalogue().items() if not a.deprecated}
    combinators = {k for s in rule["anyOf"] for k in s.get("properties", {})}
    assert {"and", "all", "or", "any", "not", "in_cohort"} <= combinators
    assert "one level" in rule["description"] and MAX_RULE_DEPTH >= 3
    assert schema["properties"]["body"]["properties"]["rules"]["$ref"] == "#/$defs/rule"
    rollout = tools["axiom_flags__rollout_draft_update"].input_schema
    assert "match" in rollout["$defs"]["rollout_rule"]["properties"]


def test_dual_no_draft_tool_applies_signs_or_opens_a_plan(modes):
    """DUAL-7: every draft verb writes only the draft: no cohort or rollout changes, nothing is held
    on the approval queue, no plan is opened or approved, and none of them is destructive."""
    from axiom.extensions.builtins.features.skills import bind
    from axiom.infra.skills import SkillRegistry

    state, ts = modes["state"], modes["targeting"]
    for surface in ("mcp", "chat", "cli"):
        for kind, key, body in (("cohort", "pilots", COHORT), ("rollout", "beta_editor", ROLLOUT)):
            assert run(f"flags.{kind}_draft_create", {"key": key, "body": body}, state, actor="@ana:acct",
                       surface=surface).ok
            assert run(f"flags.{kind}_draft_update", {"key": key, "body": body}, state, actor="@ana:acct",
                       surface=surface).ok
            assert run(f"flags.{kind}_draft_get", {"key": key}, state, actor="@ana:acct", surface=surface).ok
            assert run(f"flags.{kind}_draft_discard", {"key": key}, state, actor="@ana:acct", surface=surface).ok
    assert ts.cohort("pilots") is None and ts.rollout("beta_editor", "development") is None
    assert _held(state) == [] and ts.changes() == []
    reg = SkillRegistry()
    bind(reg)
    for name in DRAFT_SKILLS:
        assert reg.spec(name).effective_risk != "destructive"
    out = mcp("axiom_flags__cohort_draft_create", {"key": "pilots", "body": COHORT, "plan_hash": "x" * 32}, state)
    assert not out["ok"] and "plan_hash" in out["errors"][0]  # a draft takes no plan at all
    assert _held(state) == []


# ------------------------------------------------------------------ the builder's live count (SW-A29)


@pytest.mark.acceptance("SW-A29")
def test_sw_a29_the_live_count_says_who_and_why_and_opens_nothing(modes, monkeypatch):
    """SW-A29: a count over the rules as typed: how many, a sample with why each one is in, how many
    are unknown and which attribute had no data, over which population; it opens no plan."""
    modes["targeting"].put_cohort("others", principals=["gus", "hal"], by="@t")
    r = _admin(monkeypatch).post("/api/v1/flags/cohorts/pilots/count",
                                 json={"rules": {"attr": "locale", "op": "eq", "value": "en-US"},
                                       "principals": ["dee"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["count"] == 1 and out["sample"] == [{"who": "dee", "why": "listed"}]
    assert out["unknown"]["count"] == 2 and "locale" in out["unknown"]["why"]
    assert out["population"]["size"] == 3 and "sites" in out
    assert _held(modes["state"]) == []
    viewer = web(monkeypatch, sub="pat", roles=("viewer",))
    assert viewer.post("/api/v1/flags/cohorts/pilots/count", json={"principals": ["x"]}).status_code == 403


@pytest.mark.acceptance("SW-A29")
def test_sw_a29_a_matching_value_is_the_why(modes, monkeypatch):
    """SW-A29: someone a rule admits says which value admitted them."""
    modes["targeting"].put_cohort("others", principals=["gus"], by="@t")
    out = _admin(monkeypatch).post("/api/v1/flags/cohorts/pilots/count",
                                   json={"rules": {"attr": "principal", "op": "in", "value": ["gus"]}}).json()
    assert out["sample"] == [{"who": "gus", "why": "principal is gus"}] and out["unknown"] is None


# ------------------------------------------------------------------ the builder's Check step (SW-A30)


@pytest.mark.acceptance("SW-A30")
def test_sw_a30_the_cohort_preview_says_who_joins_who_leaves_and_what_reads_it(people, monkeypatch):
    """SW-A30: the cohort preview's ``affected`` adds joins, leaves, a sample of who and which way,
    the sites when known, and every flag whose rollout or stage plan reads the cohort, keeping
    count, unknown, population and approvals_needed."""
    ts = people["targeting"]
    ts.put_cohort("pilots", principals=["ana", "cy"], by="@t")
    ts.put_cohort("wider", rules={"in_cohort": "pilots"}, by="@t")
    ts.put_rollout("beta_editor", "development", rules=[{"cohort": "wider", "value": "on"}], reason="r", by="@t")
    body = {"principals": ["ana", "bob"], "reason": "swap cy for bob"}
    r = _admin(monkeypatch).post("/api/v1/flags/cohorts/pilots/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    affected = out["affected"]
    assert affected["joins"] == 1 and affected["leaves"] == 1
    assert sorted(affected["sample"], key=lambda s: s["who"]) == [{"who": "bob", "change": "joins"},
                                                                  {"who": "cy", "change": "leaves"}]
    assert affected["read_by"] == [{"flag": "beta_editor", "description": "the new editor",
                                    "environment": "development"}]
    assert "sites" in affected
    for key in ("count", "unknown", "population"):
        assert key in affected
    assert out["approvals_needed"] == 0


# ------------------------------------------------------------------ the builder's vocabulary


def test_dual_the_builder_reads_the_attribute_vocabulary_from_the_server(modes, monkeypatch):
    """DUAL-8: ``GET /flags/attributes`` lists every attribute a rule may name, grouped Person,
    Place, Time or Cohort, with a label, a description and whether this node can resolve it."""
    from axiom.extensions.builtins.features import attributes

    r = _admin(monkeypatch).get("/api/v1/flags/attributes")
    assert r.status_code == 200, r.text
    rows = {a["name"]: a for a in r.json()}
    assert {n for n, a in attributes.catalogue().items() if not a.deprecated} | {"in_cohort"} == set(rows)
    for row in rows.values():
        assert set(row) == {"name", "label", "group", "description", "available", "has_values"}
        assert row["group"] in ("Person", "Place", "Time", "Cohort")
    assert rows["region"]["group"] == "Person" and rows["region"]["label"] == "Region"
    assert rows["site_region"]["group"] == "Place" and rows["site_time"]["group"] == "Time"
    assert rows["in_cohort"]["group"] == "Cohort" and rows["account_kind"]["available"] is False
    assert rows["principal"]["available"] is True
    assert rows["role"]["has_values"] is True and rows["site_time"]["has_values"] is False


# ------------------------------------------------------------------ values are picked (SW-A26)


def _values(state, attribute, **kw):
    out = run("flags.values", {"attribute": attribute, **kw}, state, actor="@ana:acct")
    assert out.ok, out.errors
    return out.value


@pytest.mark.acceptance("SW-A26")
def test_sw_a26_each_attribute_names_where_its_values_come_from(modes):
    """SW-A26 (owner decision Q12): a builder picks a value from a known list and types one only
    where no list exists: roles from the role table, places from the site configuration, zones from
    the tz database, cohorts from the cohort list, people and accounts from the directory and what
    this node knows, weekdays fixed, local time typed."""
    from axiom.extensions.builtins.features import attributes

    state = modes["state"]
    attributes.set_site_settings({"site.region": "US-TX", "site.country": "US"})
    modes["targeting"].put_cohort("pilots", name="Pilots", principals=["dee"], accounts=["acme"], by="@t")

    role = _values(state, "role")
    assert role["source"] == "roles" and {"value": "site-admin", "label": "Site admin"} in role["values"]
    assert {"value": "admin", "label": "Access admin"} in role["values"]
    assert {v["value"] for v in _values(state, "site_weekday")["values"]} == {
        "mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    hour = _values(state, "site_time")
    assert hour == {"attribute": "site_time", "source": None, "values": [], "more": False}
    zones = _values(state, "timezone", query="chicago")
    assert zones["source"] == "tz" and {"value": "America/Chicago", "label": "America/Chicago"} in zones["values"]
    assert _values(state, "in_cohort")["values"] == [{"value": "pilots", "label": "Pilots"}]
    assert _values(state, "region")["values"] == [{"value": "US-TX", "label": "US-TX"}]
    assert _values(state, "site_country")["source"] == "sites"
    people = _values(state, "principal")
    assert people["source"] == "directory" and {"value": "dee", "label": "dee"} in people["values"]
    assert {"value": "acme", "label": "acme"} in _values(state, "account")["values"]
    capped = _values(state, "timezone", limit=3)
    assert len(capped["values"]) == 3 and capped["more"] is True
    out = run("flags.values", {"attribute": "nope"}, state)
    assert not out.ok and "no attribute 'nope'" in out.errors[0]


@pytest.mark.acceptance("SW-A26")
def test_sw_a26_an_extension_attribute_declares_its_own_list(modes):
    """SW-A26: an extension's attribute names its values (a list, or a function of the query)."""
    from axiom.extensions.builtins.features import attributes
    from axiom.extensions.builtins.features.attributes import AttributeDef, parse_manifest_attributes

    attributes.set_extension_attributes([AttributeDef("plan_tier", "the account's plan", values=("free", "pro"))])
    out = _values(modes["state"], "plan_tier")
    assert out["source"] == "extension" and out["values"] == [{"value": "free", "label": "free"},
                                                              {"value": "pro", "label": "pro"}]
    (parsed,) = parse_manifest_attributes(
        {"flag_attributes": [{"name": "tier", "description": "d", "resolver": "m:f", "values": ["a"]}]}, owner="x")
    assert parsed.values == ("a",)


@pytest.mark.acceptance("SW-A26")
def test_sw_a26_values_on_every_surface(modes, monkeypatch, capsys):
    """SW-A26: ``GET /flags/values/{attribute}?q=&limit=``, ``flags values ATTR [QUERY]`` and the MCP
    tool, whose description tells a model to map a person's words to a listed value and to ask one
    question when it cannot."""
    from axiom.extensions.builtins.features.skills import bind
    from axiom.extensions.builtins.flags import cli
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillRegistry

    r = _admin(monkeypatch).get("/api/v1/flags/values/role?q=admin&limit=1")
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "roles" and len(r.json()["values"]) == 1 and r.json()["more"] is True
    assert _admin(monkeypatch).get("/api/v1/flags/values/nope").status_code == 404
    assert cli.main(["values", "timezone", "chicago"]) == 0
    assert "America/Chicago" in capsys.readouterr().out
    reg = SkillRegistry()
    bind(reg)
    tool = next(t for t in skill_tool_contribution(reg, ctx_factory=lambda: None).tools
                if t.name == "axiom_flags__values")
    text = tool.description.lower()
    assert "up north" in text and "ask one question" in text
