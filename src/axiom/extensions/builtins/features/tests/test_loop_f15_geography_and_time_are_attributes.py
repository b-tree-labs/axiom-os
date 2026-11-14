# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F15: time zone, country and region of a principal and of the site and node, and the
subject's local time of day and day of week, are attributes a rule can read. Nothing is
invented: an attribute nobody configured is UNKNOWN."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.features import attributes
from axiom.extensions.builtins.features.tests.conftest import svc, web

WORKING_HOURS = {"and": [
    {"attr": "local_hour", "op": "gte", "value": 9},
    {"attr": "local_hour", "op": "lt", "value": 17},
    {"attr": "day_of_week", "op": "in", "value": ["mon", "tue", "wed", "thu", "fri"]},
]}


def _attrs(**who):
    look = attributes.lookup_for(svc().subject(**who))
    return {n: look(n) for n in ("timezone", "country", "region", "site_timezone", "site_country",
                                 "site_region", "node_timezone", "node_country", "node_region",
                                 "local_timezone", "local_hour", "day_of_week")}


@pytest.mark.acceptance("LOOP-F15")
def test_the_catalogue_declares_geography_and_time(modes):
    names = set(attributes.catalogue())
    assert {"timezone", "country", "region", "site_timezone", "site_country", "site_region",
            "node_timezone", "node_country", "node_region", "local_timezone", "local_hour",
            "day_of_week"} <= names


@pytest.mark.acceptance("LOOP-F15")
def test_nothing_configured_is_unknown_never_a_guess(modes):
    a = _attrs(user="ana")
    for name, attr in a.items():
        assert attr.value is None, f"{name} was invented: {attr.value!r}"


@pytest.mark.acceptance("LOOP-F15")
def test_a_principal_country_and_region_target_a_rule(modes):
    ts = modes["targeting"]
    ts.put_cohort("dach", rules={"attr": "country", "op": "in", "value": ["DE", "AT", "CH"]}, by="@t")
    ts.put_rollout("beta_editor", "development", rules=[{"cohort": "dach", "value": "on"}], reason="r", by="@t")
    s = svc()
    assert s.resolve_flag("beta_editor", user="ana", country="de") == "on"
    assert s.resolve_flag("beta_editor", user="bob", country="FR") == "off"
    out = s.explain("beta_editor", user="cy")
    assert out["value"] == "off" and "country" in out["unknown_attributes"]


@pytest.mark.acceptance("LOOP-F15")
def test_site_and_node_geography_come_from_their_configuration(modes, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE_TZ", "Europe/Berlin")
    attributes.set_site_settings({"site.country": "DE", "site.region": "DE-BY"})
    modes["node"].write_text('[node]\nfunctions = []\n\n[settings]\n"node.timezone" = "Pacific/Auckland"\n'
                             '"node.country" = "NZ"\n"node.region" = "NZ-AUK"\n')
    a = _attrs(node="node-a")
    assert (a["site_timezone"].value, a["site_country"].value, a["site_region"].value) == (
        "Europe/Berlin", "DE", "DE-BY")
    assert (a["node_timezone"].value, a["node_country"].value, a["node_region"].value) == (
        "Pacific/Auckland", "NZ", "NZ-AUK")
    assert _attrs(node="another-node")["node_country"].value is None  # this node's file says nothing of it


@pytest.mark.acceptance("LOOP-F15")
def test_a_time_zone_that_is_not_in_the_tz_database_is_unknown(modes, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE_TZ", "Mars/Olympus")
    a = _attrs(user="ana", timezone="Atlantis/Lost")
    assert a["timezone"].value is None and a["site_timezone"].value is None and a["local_hour"].value is None


@pytest.mark.acceptance("LOOP-F15")
def test_only_during_local_working_hours_follows_daylight_saving(modes):
    modes["targeting"].put_rollout("beta_editor", "development",
                                   rules=[{"match": WORKING_HOURS, "value": "on"}], reason="r", by="@t")
    s = svc()

    def at(iso):
        return s.explain("beta_editor", user="ana", timezone="America/Chicago",
                         now=datetime.fromisoformat(iso).replace(tzinfo=UTC))["value"]

    assert at("2026-07-01T14:30:00") == "on"    # 09:30 CDT, a Wednesday
    assert at("2026-12-02T14:30:00") == "off"   # 08:30 CST: the same UTC time is before nine in winter
    assert at("2026-12-02T15:30:00") == "on"    # 09:30 CST
    assert at("2026-07-04T15:00:00") == "off"   # a Saturday


@pytest.mark.acceptance("LOOP-F15")
def test_local_time_falls_back_from_principal_to_node_to_site(modes, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE_TZ", "Asia/Tokyo")
    now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    s = svc()
    out = s.explain("beta_editor", user="ana", now=now)
    assert out["subject"]["local_timezone"] == "Asia/Tokyo"
    modes["node"].write_text('[node]\nfunctions = []\n\n[settings]\n"node.timezone" = "Europe/London"\n')
    assert s.explain("beta_editor", user="ana", now=now)["subject"]["local_timezone"] == "Europe/London"
    out = s.explain("beta_editor", user="ana", timezone="America/Denver", now=now)
    assert out["subject"]["local_timezone"] == "America/Denver" and out["subject"]["local_hour"] == 18.0


@pytest.mark.acceptance("LOOP-F15")
def test_the_web_reads_the_principal_zone_and_country_from_the_session(modes, monkeypatch):
    from axiom.extensions.builtins.features import api

    client = web(monkeypatch, sub="ana")
    monkeypatch.setattr(api, "_claims", lambda request: {
        "sub": "ana", "roles": [], "site": "acct", "zoneinfo": "Europe/Paris",
        "address": {"country": "FR", "region": "FR-IDF"}})
    r = client.get("/api/v1/features/beta_editor/explain")
    assert r.status_code == 200, r.text
    subject = r.json()["subject"]
    assert (subject["timezone"], subject["country"], subject["region"]) == ("Europe/Paris", "FR", "FR-IDF")
