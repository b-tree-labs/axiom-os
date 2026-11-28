# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Q13 (owner decision), SW-A28: a time rule says whose clock it reads.

``site_time`` / ``site_weekday`` read the site's time zone; ``my_time`` / ``my_weekday`` read the
person's own clock, and only when a rule names them. No fallback chain decides what a rule
means. A site-time rule on a site with no time zone is refused when it is saved, naming the
setting to fix. The old names (``local_hour``, ``day_of_week``, ``local_time``, ``weekday``) still
resolve, as site time, and say they are deprecated.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.features import attributes
from axiom.extensions.builtins.features.tests.conftest import run, svc, web

NOON_UTC_WEDNESDAY = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _look(at=None, **who):
    from dataclasses import replace

    look = attributes.lookup_for(replace(svc().subject(**who), at=at))
    return {n: look(n).value for n in ("site_time", "site_weekday", "my_time", "my_weekday", "local_hour",
                                       "day_of_week", "local_time", "weekday")}


@pytest.mark.acceptance("SW-A28")
def test_q13_site_time_reads_the_site_and_my_time_reads_the_person(modes, monkeypatch):
    """Q13: two explicit clocks; neither falls back to the other or to the node."""
    monkeypatch.setenv("AXIOM_SITE_TZ", "Asia/Tokyo")
    modes["node"].write_text('[node]\nfunctions = []\n\n[settings]\n"node.timezone" = "Europe/London"\n')
    got = _look(user="ana", timezone="America/Denver", at=NOON_UTC_WEDNESDAY)
    assert (got["site_time"], got["site_weekday"]) == (21.0, "wed")
    assert (got["my_time"], got["my_weekday"]) == (6.0, "wed")
    # The old names are site time now, whatever the person's or the node's zone.
    assert got["local_hour"] == got["local_time"] == 21.0 and got["day_of_week"] == got["weekday"] == "wed"
    no_zone = _look(user="ana", at=NOON_UTC_WEDNESDAY)
    assert no_zone["my_time"] is None and no_zone["my_weekday"] is None and no_zone["site_time"] == 21.0


@pytest.mark.acceptance("SW-A28")
def test_q13_a_site_time_rule_on_a_site_with_no_time_zone_is_refused_at_save(modes, monkeypatch):
    """Q13: check_cohort, check_rollout and every draft write refuse a site-time rule while the site
    has no time zone, and say which setting to set; a person's-clock rule needs none."""
    from axiom.extensions.builtins.features import targeting as t

    ts = modes["targeting"]
    office = {"attr": "site_time", "op": "gte", "value": 9}
    for check in (lambda: ts.check_cohort("office", rules=office),
                  lambda: ts.check_rollout([{"match": office, "value": "on"}], flag=None),
                  lambda: ts.check_cohort("office", rules={"attr": "day_of_week", "op": "eq", "value": "mon"})):
        with pytest.raises(t.RuleError, match=r"site\.timezone.*AXIOM_SITE_TZ"):
            check()
    out = run("flags.cohort_draft_create", {"key": "office", "body": {"rules": office}}, modes["state"],
              actor="@ana:acct")
    assert not out.ok and "site.timezone" in out.errors[0]
    ts.check_cohort("mine", rules={"attr": "my_time", "op": "gte", "value": 9})
    monkeypatch.setenv("AXIOM_SITE_TZ", "America/Chicago")
    ts.check_cohort("office", rules=office)


@pytest.mark.acceptance("SW-A28")
def test_q13_explain_says_which_clock_decided(modes, monkeypatch):
    """Q13: explain names the clock each time attribute the flag's rules read, and its zone; an old
    name carries a note that it is site time and deprecated."""
    monkeypatch.setenv("AXIOM_SITE_TZ", "America/Chicago")
    ts = modes["targeting"]
    ts.put_rollout("beta_editor", "development", reason="r", by="@t", rules=[
        {"match": {"and": [{"attr": "site_time", "op": "gte", "value": 9},
                           {"attr": "my_weekday", "op": "in", "value": ["mon", "tue", "wed"]},
                           {"attr": "local_hour", "op": "lt", "value": 17}]}, "value": "on"}])
    out = svc().explain("beta_editor", user="ana", timezone="Europe/Paris", now=NOON_UTC_WEDNESDAY)
    clocks = {c["attribute"]: c for c in out["clocks"]}
    assert clocks["site_time"]["clock"] == "site" and clocks["site_time"]["zone"] == "America/Chicago"
    assert clocks["my_weekday"]["clock"] == "person" and clocks["my_weekday"]["zone"] == "Europe/Paris"
    assert clocks["local_hour"]["clock"] == "site" and "deprecated" in clocks["local_hour"]["note"]
    # The skill's answer (every surface) carries the same.
    via_skill = run("flags.explain", {"key": "beta_editor", "user": "ana", "timezone": "Europe/Paris"},
                    modes["state"]).value
    assert {c["attribute"] for c in via_skill["clocks"]} == {"site_time", "my_weekday", "local_hour"}


@pytest.mark.acceptance("SW-A28")
def test_q13_the_builder_lists_the_two_clocks_and_not_the_old_names(modes, monkeypatch):
    """Q13: the vocabulary lists Site time, Site weekday, Your time and Your weekday (group Time),
    not the old names; weekdays have a list, times have none; an old name still answers values with
    a deprecation note."""
    rows = {a["name"]: a for a in web(monkeypatch, sub="ana", roles=("site-admin",)).get(
        "/api/v1/flags/attributes").json()}
    expected = {"site_time": "Site time", "site_weekday": "Site weekday", "my_time": "Your time",
                "my_weekday": "Your weekday"}
    for name, label in expected.items():
        assert rows[name]["label"] == label and rows[name]["group"] == "Time"
    for old in ("local_hour", "day_of_week", "local_time", "weekday"):
        assert old not in rows
    for name in ("site_weekday", "my_weekday"):
        out = run("flags.values", {"attribute": name}, modes["state"]).value
        assert out["source"] == "weekdays" and len(out["values"]) == 7
    for name in ("site_time", "my_time"):
        assert run("flags.values", {"attribute": name}, modes["state"]).value["source"] is None
    old = run("flags.values", {"attribute": "day_of_week"}, modes["state"]).value
    assert old["source"] == "weekdays" and "site_weekday" in old["note"]


@pytest.mark.acceptance("SW-A28")
def test_q13_the_mcp_rule_schema_names_the_two_clocks(modes):
    """Q13: the draft tools' rule schema offers the explicit clocks and says what each reads."""
    from axiom.extensions.builtins.features.skills import bind
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    tool = next(t for t in skill_tool_contribution(reg, ctx_factory=lambda: None).tools
                if t.name == "axiom_flags__cohort_draft_update")
    rule = tool.input_schema["$defs"]["rule"]
    leaf = next(s for s in rule["anyOf"] if "attr" in s.get("properties", {}))
    names = set(leaf["properties"]["attr"]["enum"])
    assert {"site_time", "site_weekday", "my_time", "my_weekday"} <= names
    assert not names & {"local_hour", "day_of_week", "local_time", "weekday"}
    text = rule["description"]
    assert "site_time" in text and "my_time" in text and "site.timezone" in text
