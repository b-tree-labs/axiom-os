# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F19: any rollout change can be scheduled for a time in a stated time zone, and is
previewed and approved exactly like an immediate one; the platform scheduler applies it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from axiom.extensions.builtins.flags import cli
from axiom.extensions.builtins.features.tests.conftest import mcp, run, svc
from axiom.infra import audit_trail

ZONE = "America/Chicago"


def _local(delta: timedelta) -> str:
    """A wall-clock time in ZONE, ``delta`` from now, with no offset written."""
    return (datetime.now(UTC) + delta).astimezone(ZoneInfo(ZONE)).replace(tzinfo=None).isoformat(
        timespec="minutes")


def _tick(modes, delta: timedelta):
    out = run("flags.rollout_tick", {"now": (datetime.now(UTC) + delta).isoformat()}, modes["state"],
              actor="@pulse")
    assert out.ok, out.errors
    return out.value


@pytest.mark.acceptance("LOOP-F19")
def test_a_rollout_change_is_scheduled_in_a_stated_zone_and_applied_when_due(modes):
    at = _local(timedelta(hours=3))
    out = run("flags.rollout_set", {"key": "beta_editor", "rules": [{"value": "on"}], "reason": "launch",
                                       "at": at, "timezone": ZONE}, modes["state"])
    assert out.ok, out.errors
    v = out.value
    assert v["applied"] is False and v["scheduled"]["timezone"] == ZONE
    due = datetime.fromisoformat(v["scheduled"]["run_at"])
    assert due.astimezone(ZoneInfo(ZONE)).replace(tzinfo=None).isoformat(timespec="minutes") == at
    assert modes["targeting"].rollout("beta_editor", "development") is None
    assert svc().resolve_flag("beta_editor", user="ana") == "off"

    assert _tick(modes, timedelta(hours=1))["applied"] == []
    assert _tick(modes, timedelta(hours=4))["applied"] == [v["scheduled"]["id"]]
    assert svc().resolve_flag("beta_editor", user="ana") == "on"
    listed = run("flags.changes", {}, modes["state"]).value["changes"]
    assert listed[0]["status"] == "applied" and listed[0]["author"] == "@ana"
    recs = [r for r in audit_trail.read_chain(audit_trail.audit_path(modes["state"]))
            if r["skill"] == "features.change_apply"]
    assert len(recs) == 1 and recs[0]["change"]["new"]["rules"] == [{"value": "on"}]


@pytest.mark.acceptance("LOOP-F19")
def test_a_scheduled_change_is_previewed_like_an_immediate_one(modes, capsys):
    at = _local(timedelta(days=1))
    assert cli.main(["rollout", "set", "beta_editor", "--rules", '[{"value": "on"}]', "--reason", "go",
                     "--at", at, "--timezone", ZONE]) == 0
    out = capsys.readouterr().out
    assert "preview" in out and ZONE in out
    assert run("flags.changes", {}, modes["state"]).value["changes"] == []


@pytest.mark.acceptance("LOOP-F19")
def test_from_an_ai_client_a_scheduled_change_waits_on_a_person_like_any_other(modes):
    out = mcp("axiom_flags__rollout_set", {"key": "beta_editor", "rules": [{"value": "on"}], "reason": "r",
                                              "at": _local(timedelta(hours=2)), "timezone": ZONE}, modes["state"])
    assert not out["ok"] and out["value"]["plan_hash"]
    assert run("flags.changes", {}, modes["state"]).value["changes"] == []


@pytest.mark.acceptance("LOOP-F19")
def test_a_time_must_name_its_zone_be_in_the_future_and_exist_on_the_clock(modes):
    def refused(at, tz, match):
        out = run("flags.rollout_set", {"key": "beta_editor", "rules": [{"value": "on"}], "reason": "r",
                                           "at": at, "timezone": tz}, modes["state"])
        assert not out.ok and match in out.errors[0], out.errors

    refused(_local(timedelta(hours=2)), "", "time zone")
    refused(_local(timedelta(hours=2)), "Nowhere/Else", "time zone")
    refused(_local(-timedelta(hours=2)), ZONE, "past")
    refused("2027-03-14T02:30", ZONE, "does not exist")     # the clocks jump from 02:00 to 03:00
    refused("2027-11-07T01:30", ZONE, "happens twice")      # the clocks fall back over 01:00-02:00


@pytest.mark.acceptance("LOOP-F19")
def test_cohort_and_stage_plan_changes_schedule_too_and_a_scheduled_change_can_be_cancelled(modes):
    at = _local(timedelta(hours=2))
    out = run("flags.cohort_set", {"key": "pilots", "principals": ["ana"], "reason": "r", "at": at,
                                      "timezone": ZONE}, modes["state"])
    assert out.ok and out.value["scheduled"]
    plan = {"stages": [{"percentage": 10}], "soak": "1h", "value": "on", "gates": [], "on_fail": "halt"}
    staged = run("flags.stage_plan_set", {"key": "fast_search", "plan": plan, "reason": "r", "at": at,
                                             "timezone": ZONE}, modes["state"])
    assert staged.ok and staged.value["scheduled"]
    cancelled = run("flags.change_cancel", {"id": staged.value["scheduled"]["id"], "reason": "not yet"},
                    modes["state"])
    assert cancelled.ok
    applied = _tick(modes, timedelta(hours=3))["applied"]
    assert applied == [out.value["scheduled"]["id"]]
    assert modes["targeting"].cohort("pilots")["principals"] == ["ana"]
    assert modes["targeting"].stage_plan("fast_search", "development") is None


@pytest.mark.acceptance("LOOP-F19")
def test_a_scheduled_change_that_no_longer_applies_fails_and_says_why(modes):
    ts = modes["targeting"]
    ts.put_cohort("pilots", principals=["ana"], by="@t")
    out = run("flags.rollout_set", {"key": "beta_editor", "rules": [{"cohort": "pilots", "value": "on"}],
                                       "reason": "r", "at": _local(timedelta(hours=1)), "timezone": ZONE},
              modes["state"])
    assert out.ok
    ts.delete_cohort("pilots")
    value = _tick(modes, timedelta(hours=2))
    assert value["failed"] == [out.value["scheduled"]["id"]]
    change = run("flags.changes", {}, modes["state"]).value["changes"][0]
    assert change["status"] == "failed" and "pilots" in change["result"]["error"]
    assert len(modes["inbox"].all()) + len(modes["incidents"].in_states(("holding",))) >= 1
