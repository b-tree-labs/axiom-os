# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The fire and the sweep, unit layer (ADR-196).

SQLite stands in for both stores and a recording sender stands in for HERALD,
so the ladder, the check, the dedup and the sweep are exercised deterministically
with an injected clock. The real-process proofs are in
``test_followups_real_postgres.py``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.program import followup_clock as clock
from axiom.extensions.builtins.program import followup_engine as engine
from axiom.extensions.builtins.program import followup_store as store
from axiom.extensions.builtins.program import skills
from axiom.extensions.builtins.schedule import store as pulse_store
from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

from .conftest import ME, NOW, OTHER

DEPUTY = "@casey:example-org"
SECRET = "glpat-" + "a1B2c3D4e5F6g7H8i9J0"


class Herald:
    """A recording sender. Duplicate dedup keys are suppressed, as HERALD does."""

    def __init__(self):
        self.sent: list[dict] = []
        self.keys: set[str] = set()
        self.unreachable: set[str] = set()

    def __call__(self, recipient, subject, body, dedup_key, priority):
        if dedup_key in self.keys:
            return engine.Sent(ok=True, channel=None, durable=True, suppressed=True, receipt_id="dup")
        self.keys.add(dedup_key)
        self.sent.append({"to": recipient, "subject": subject, "body": body, "key": dedup_key, "priority": priority})
        if recipient in self.unreachable:
            return engine.Sent(ok=True, channel="inbox", durable=False, receipt_id=f"r{len(self.sent)}")
        return engine.Sent(ok=True, channel="email", durable=True, receipt_id=f"r{len(self.sent)}")


@pytest.fixture
def herald():
    return Herald()


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setenv(engine.OPERATOR_ENV, DEPUTY)


def _registry():
    r = SkillRegistry()
    skills.bind(r)
    r.register_skill(SkillSpec(
        name="example.status",
        fn=lambda p, c: SkillResult(ok=True, value={
            "state": "ready", "rows": 3, "password": "hunter2", "nested": {"lag_s": 4, "api_token": "xyz"},
            "note": "key is " + SECRET}),
        description="status", inputs={"target": "str"}, side_effects=False, risk="read"))
    return r


def _add(world, **kw):
    kw.setdefault("what", "check on the drop-old task")
    kw.setdefault("due", "2h")
    r = world.call("followup_add", **kw)
    assert r.ok, r.errors
    return r.value["id"]


def _fire(fid, herald, at, tmp_path, registry=None):
    return engine.fire(fid, registry=registry or _registry(), state_dir=tmp_path, now=at, sender=herald)


def test_the_first_fire_runs_the_check_and_the_reminder_carries_its_state_and_the_next_command(world, herald, tmp_path):
    fid = _add(world, check="example.status", check_args={"target": "orders_old"},
               next_command="axi data drop-old orders_old")
    out = _fire(fid, herald, NOW + timedelta(hours=2), tmp_path)
    assert out["outcome"] == "delivered" and out["recipient"] == ME
    [m] = herald.sent
    assert m["to"] == ME and m["subject"].startswith("Follow-up due: check on the drop-old task [")
    # the summary alone (all an inbox keeps) carries the check's state, the next command and how to ack
    assert "check example.status passed (state=ready, rows=3, nested.lag_s=4, " in m["subject"]
    assert "next: axi data drop-old orders_old" in m["subject"] and f"ack: axi program followup ack --id {fid}" in m["subject"]
    assert "\n" not in m["subject"] and len(m["subject"]) <= engine.SUMMARY_MAX
    assert "Check (example.status): passed" in m["body"] and "state: ready" in m["body"] and "rows: 3" in m["body"]
    assert "nested.lag_s: 4" in m["body"]
    assert "Next command:\n  axi data drop-old orders_old" in m["body"]
    assert f"axi program followup ack --id {fid}" in m["body"]
    row = store.get(fid)
    assert (row["state"], row["rung"]) == ("due", 1)
    assert row["next_action_at"] == NOW + timedelta(hours=2, seconds=6 * 3600)


def test_no_secret_value_reaches_the_message_or_the_journal(world, herald, tmp_path):
    fid = _add(world, check="example.status", check_args={"target": "t"})
    _fire(fid, herald, NOW + timedelta(hours=2), tmp_path)
    everything = repr(herald.sent) + repr(store.events(fid)) + repr(store.get(fid))
    for secret in ("hunter2", "xyz", SECRET):
        assert secret not in everything, secret
    assert "[redacted:gitlab-pat]" in herald.sent[0]["body"], "a credential-format value is replaced by a typed marker"
    [ev] = [e for e in store.events(fid) if e["kind"] == "check_ran"]
    assert ev["detail"]["redactions"] == 1 and "password" not in ev["detail"]["fields"]


def test_a_check_that_is_no_longer_read_only_is_not_run(world, herald, tmp_path):
    fid = _add(world, check="example.status", check_args={"target": "t"})
    reg = _registry()
    object.__setattr__(reg.spec("example.status"), "side_effects", True)
    _fire(fid, herald, NOW + timedelta(hours=2), tmp_path, registry=reg)
    assert "no longer declared read-only" in herald.sent[0]["body"]
    assert "state: ready" not in herald.sent[0]["body"]


def test_the_ladder_climbs_on_silence_and_each_rung_goes_to_its_audience(world, herald, operator, tmp_path):
    fid = _add(world, what="drop the old table")
    # asked by ME and owed by ME: the requester rung has no new audience and is skipped
    t = NOW + timedelta(hours=2)
    steps = []
    for _ in range(4):
        steps.append(_fire(fid, herald, t, tmp_path)["outcome"])
        t = max(t, store.get(fid)["next_action_at"])
    assert steps == ["delivered", "delivered", "skipped", "delivered"]
    assert [(m["to"], m["priority"]) for m in herald.sent] == [(ME, "normal"), (ME, "normal"), (DEPUTY, "high")]
    assert "Follow-up unanswered, please contact" in herald.sent[2]["subject"]


def test_a_requester_who_is_not_the_owner_is_told_at_the_flag_rung(world, herald, operator, tmp_path):
    fid = _add(world, what="x")
    store.update(fid, owner=OTHER)  # asked by ME, owed by OTHER
    t = NOW + timedelta(hours=2)
    for _ in range(4):
        _fire(fid, herald, t, tmp_path)
        t = max(t, store.get(fid)["next_action_at"] or t)
    assert [m["to"] for m in herald.sent] == [OTHER, OTHER, ME, DEPUTY]
    assert "not acknowledged by" in herald.sent[2]["subject"]


def test_ack_stops_the_ladder(world, herald, tmp_path):
    fid = _add(world)
    _fire(fid, herald, NOW + timedelta(hours=2), tmp_path)
    assert world.call("followup_ack", id=fid).ok
    assert _fire(fid, herald, NOW + timedelta(hours=9), tmp_path)["outcome"] == "nothing_to_do"
    assert len(herald.sent) == 1
    assert store.get(fid)["state"] == "acknowledged"


def test_a_rerun_after_a_crash_between_send_and_record_delivers_once(world, herald, tmp_path):
    fid = _add(world)
    at = NOW + timedelta(hours=2)
    # The crashed run: attempt journaled and the message sent, but "delivered" never recorded.
    store.add_event(fid, "attempt", engine.ACTOR, now=at, cycle=0, rung=0, dedup_key=f"attempt:{fid}:0:0")
    herald(ME, "s", "b", f"followup:{fid}:0:0", "normal")
    assert len(herald.sent) == 1
    out = _fire(fid, herald, at, tmp_path)
    assert out["outcome"] == "delivered" and len(herald.sent) == 1, "HERALD suppressed the duplicate"
    ev = [e for e in store.events(fid) if e["kind"] == "delivered"]
    assert len(ev) == 1 and ev[0]["detail"]["suppressed_duplicate"] is True
    # a re-run after "delivered" was recorded only advances
    store.update(fid, rung=0, next_action_at=at, state="open")
    assert _fire(fid, herald, at, tmp_path)["outcome"] == "advanced_after_delivery"
    assert len(herald.sent) == 1


def test_an_unreachable_owner_is_surfaced_to_the_requester_and_the_operator_at_once(world, herald, operator, tmp_path):
    fid = _add(world)
    store.update(fid, owner=OTHER)
    herald.unreachable.add(OTHER)
    out = _fire(fid, herald, NOW + timedelta(hours=2), tmp_path)
    assert out["reach"] == "unreachable"
    assert [m["to"] for m in herald.sent] == [OTHER, ME, DEPUTY]
    assert "could not be reached on any channel" in herald.sent[1]["body"]
    kinds = [e["kind"] for e in store.events(fid)]
    assert "unreachable" in kinds and kinds.count("escalated") == 2


def test_the_horizon_expires_an_unanswered_followup_and_it_stays_owed(world, herald, operator, tmp_path):
    fid = _add(world)
    t = NOW + timedelta(hours=2)
    for _ in range(8):
        if _fire(fid, herald, t, tmp_path)["outcome"] == "expired":
            break
        t = max(t, store.get(fid)["next_action_at"])
    assert store.get(fid)["state"] == "expired"
    assert [x["id"] for x in world.call("followup_owed").value["expired"]] == [fid]


def test_an_early_stale_fire_after_a_snooze_does_nothing(world, herald, tmp_path):
    fid = _add(world)
    world.call("followup_snooze", id=fid, by="48h")
    assert _fire(fid, herald, NOW + timedelta(hours=2), tmp_path)["outcome"] == "not_yet"
    assert herald.sent == []


def test_the_sweep_rearms_a_follow_up_whose_clock_row_was_lost_and_marks_the_never_delivered(world):
    fid = _add(world)
    sid = store.get(fid)["schedule_id"]
    clock.disarm(sid)  # the row is gone (a reset scheduler, a dropped schema)
    later = NOW + timedelta(hours=3)
    out = engine.sweep(now=later)
    assert out["rearmed"] == [fid] and out["missed"] == [fid] and out["quiet"] is False
    row = store.get(fid)
    assert row["schedule_id"] != sid
    with pulse_store.session_scope() as s:
        assert s.get(ScheduleDefinition, row["schedule_id"]).state == "active"
    assert engine.sweep(now=later)["quiet"] is True, "a second sweep finds nothing and writes nothing"


def test_the_sweep_is_quiet_with_nothing_to_do(world):
    assert engine.sweep(now=NOW) == {"rearmed": [], "expired": [], "missed": [], "quiet": True}
    assert store.all_rows() == []


def test_the_sweep_heartbeat_is_registered_once(world):
    a, b = clock.ensure_sweep(now=NOW), clock.ensure_sweep(now=NOW)
    assert a == b
    with pulse_store.session_scope() as s:
        d = s.get(ScheduleDefinition, a)
        assert (d.action, d.cadence_kind, d.reentrant) == ("program.followup_sweep", "interval", True)
